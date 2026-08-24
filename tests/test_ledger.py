"""The run ledger: promotion, recovery, session reuse and measured effort.

Every test here is hermetic. ``RECKON_HOME`` moves the transient crew directory
into a temp tree, the repository is a real but throwaway git repo, and no test
spawns a harness — the one dispatch path that would substitutes a launcher. The
split under test is the point of most assertions: nothing transient may land in
a working tree, and nothing durable may live only in the cache.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import _store, calibration, capabilities, crew, ledger
from reckon.cli import main as cli_main


CONFIG = {
    "default_backend": "alpha",
    "backends": {
        "alpha": {
            "launch": "cli",
            "command": "codex",
            "model": "some-model",
            "effort": "high",
            "sandbox": "worktree-full",
            "session_reuse": True,
            "time_budget": "25m",
        },
        "native": {"launch": "in-harness", "time_budget": "25m"},
    },
    "roles": {"implement": {}, "inline": {"backend": "native"}},
    "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
}

FIXTURES = Path(__file__).parent / "fixtures" / "backends"
PROJECT = "proj"
SESSION_ID = "019ff509-8a60-7723-94fd-65942a6d8faa"


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """Point the transient crew directory at a temp tree."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home


@pytest.fixture()
def repo(tmp_path):
    """A throwaway git repository with a docs tree and the fleet script."""
    root = tmp_path / "repo"
    (root / "skills" / "reckon-ship" / "scripts").mkdir(parents=True)
    source = (
        Path(__file__).parents[1]
        / "skills"
        / "reckon-ship"
        / "scripts"
        / "worktree_fleet.py"
    )
    (root / "skills" / "reckon-ship" / "scripts" / "worktree_fleet.py").write_text(
        source.read_text()
    )
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    (root / "docs" / "state" / PROJECT / "index.json").write_text(
        json.dumps({"project": PROJECT, "data": {"_version": 0}}) + "\n"
    )
    (root / "reckon").mkdir()
    (root / "reckon" / "target.py").write_text("value = 1\n")
    (root / "other.py").write_text("outside = 1\n")
    for args in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "worker@example.invalid"],
        ["config", "user.name", "Worker"],
        ["add", "docs", "skills", "reckon", "other.py"],
        ["commit", "-q", "-m", "chore: seed"],
    ):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
    return root


def _node(**overrides) -> crew.TaskNode:
    fields = {
        "id": "node-a",
        "goal": "record the launch matrix for one backend",
        "plan": "plan-a",
        "section": "§3",
        "done_when": "uv run pytest tests/test_backends.py reports 28 passed",
        "write_paths": ["reckon/target.py"],
        "time_budget": "20m",
    }
    fields.update(overrides)
    return crew.TaskNode(**fields)


def _dispatch(repo, *, fixture: str | None = None, **kwargs) -> dict:
    node_kwargs = kwargs.pop("node_kwargs", {})
    node_id = str(node_kwargs.get("id") or "node-a")
    record = crew.dispatch(
        node=_node(**node_kwargs),
        project=PROJECT,
        repo=repo,
        config=CONFIG,
        session=kwargs.pop("session", f"sess-{node_id}"),
        launcher=lambda plan, *, log_path, stderr_path, prompt_path: os.getpid(),
        **kwargs,
    )
    if fixture:
        Path(record["log_path"]).write_text((FIXTURES / fixture).read_text())
    return record


def _deliver(record: dict, *, status: str = "complete") -> None:
    """Write the manifest that is the worker's real delivery."""
    manifest = Path(record["manifest_path"])
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(f"node: {record['node']['id']}\nstatus: {status}\n")


def _timestamp_stream(
    record: dict,
    *,
    first: str,
    last: str,
    input_tokens: int = 100,
    output_tokens: int = 10,
    session_id: str = SESSION_ID,
) -> None:
    events = [
        json.loads(line) for line in Path(record["log_path"]).read_text().splitlines()
    ]
    events[0]["thread_id"] = session_id
    events[0]["timestamp"] = first
    events[-1]["timestamp"] = last
    events[-1]["usage"]["input_tokens"] = input_tokens
    events[-1]["usage"]["output_tokens"] = output_tokens
    Path(record["log_path"]).write_text(
        "".join(json.dumps(event) + "\n" for event in events)
    )


def _kill(record: dict) -> None:
    """Point the pointer at a pid that cannot be running."""
    pointer = json.loads(crew.pointer_path(record["run_id"]).read_text())
    pointer["pid"] = 999999999
    crew._write_json(crew.pointer_path(record["run_id"]), pointer)


def _porcelain(root: Path) -> list[str]:
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(root),
        capture_output=True,
        text=True,
        check=True,
    )
    return [line for line in result.stdout.splitlines() if line.strip()]


def _historical_record(
    repo: Path,
    run_id: str,
    *,
    completed_at: str = "2027-01-01T02:00:00Z",
    completed_at_source: str = "provided",
    worker_seconds: int = 7200,
) -> None:
    ledger.append_run(
        PROJECT,
        ledger.build_record(
            run_id=run_id,
            plan="plan-a",
            gate="passed",
            dispatched_at="2027-01-01T00:00:00Z",
            completed_at=completed_at,
            completed_at_source=completed_at_source,
            worker_seconds=worker_seconds,
        ),
        root=repo,
    )


def _historical_stream(
    home: Path,
    run_id: str,
    *,
    timestamp: str | None = None,
    mtime: str = "2027-01-01T01:00:00Z",
    resume: bool = False,
) -> Path:
    directory = home / "crew" / "runs" / run_id
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / ("resume-1.jsonl" if resume else "stream.jsonl")
    event = {"type": "result"}
    if timestamp is not None:
        event["timestamp"] = timestamp
    path.write_text(json.dumps(event) + "\n")
    instant = datetime.fromisoformat(mtime.replace("Z", "+00:00")).timestamp()
    os.utime(path, (instant, instant))
    return path


# ── Round-trip: pointer in flight, ledger on completion ─────────────────────


def test_completion_promotes_the_pointer_into_the_repositorys_ledger(
    home, repo
) -> None:
    record = _dispatch(repo, fixture="codex-turn.jsonl")
    _deliver(record)
    before = sorted(path.name for path in crew.live_dir().glob("*.json"))
    assert before == [f"{record['run_id']}.json"]
    assert ledger.member(PROJECT, record["member"], repo) is not None

    result = crew.complete(
        record["run_id"], gate="passed", commits=[record["base_sha"]]
    )

    assert result["ledger_path"] == str(repo / "docs" / "state" / PROJECT / "crew.json")
    assert result["ledger_version"] == 2
    assert result["pointer_removed"] is True
    assert sorted(path.name for path in crew.live_dir().glob("*.json")) == []
    stored = ledger.runs(PROJECT, repo)
    assert [item["run_id"] for item in stored] == [record["run_id"]]
    assert stored[0]["gate"] == "passed"
    assert stored[0]["commits"] == [record["base_sha"]]
    # The ledger is the only state file the promotion changed.
    changed = [line for line in _porcelain(repo) if "docs/state" in line]
    assert changed == [f"?? docs/state/{PROJECT}/crew.json"]


def test_promotion_reads_terminal_time_and_usage_without_observe(home, repo) -> None:
    record = _dispatch(repo, fixture="codex-turn.jsonl")
    events = [
        json.loads(line) for line in Path(record["log_path"]).read_text().splitlines()
    ]
    terminal_time = "2027-01-02T03:04:05Z"
    events[-1]["timestamp"] = terminal_time
    Path(record["log_path"]).write_text(
        "".join(json.dumps(event) + "\n" for event in events)
    )

    stored = crew.complete(record["run_id"], gate="passed")["record"]

    assert stored["completed_at"] == terminal_time
    assert stored["completed_at_source"] == "terminal_event"
    assert stored["budget"]["tokens"]["input_tokens"] == 29253
    assert stored["budget"]["tokens"]["output_tokens"] == 5
    assert stored["budget"]["tokens"]["input_tokens_cumulative"] == 29253
    assert stored["budget"]["tokens"]["output_tokens_cumulative"] == 5


def test_stream_duration_is_separate_from_stalled_wall_time(home, repo) -> None:
    record = _dispatch(repo, fixture="codex-turn.jsonl")
    _timestamp_stream(
        record,
        first="2027-01-02T01:57:00Z",
        last="2027-01-02T02:42:00Z",
    )
    pointer = crew.read_pointer(record["run_id"])
    pointer["created_at"] = "2027-01-01T00:00:00Z"
    crew._write_json(crew.pointer_path(record["run_id"]), pointer)

    stored = crew.complete(record["run_id"], gate="passed")["record"]

    assert stored["worker_seconds"] == 45 * 60
    assert stored["worker_seconds_source"] == "stream_events"
    assert stored["wall_seconds"] == 26 * 3600 + 42 * 60
    assert stored["stalled"] is True
    report = ledger.effort_report(PROJECT, root=repo, declared={"plan-a": "M"})
    assert report["excluded_stalled"] == 1
    assert report["plans"][0]["runs"] == 0


def test_session_cumulative_tokens_become_summable_run_deltas(home, repo) -> None:
    first = _dispatch(
        repo,
        fixture="codex-turn.jsonl",
        session="token-session",
        node_kwargs={"id": "token-first"},
    )
    _timestamp_stream(
        first,
        first="2027-01-01T00:00:00Z",
        last="2027-01-01T00:01:00Z",
        input_tokens=100,
        output_tokens=10,
    )
    first_stored = crew.complete(first["run_id"], gate="passed")["record"]

    second = _dispatch(
        repo,
        fixture="codex-turn.jsonl",
        session="token-session",
        node_kwargs={"id": "token-second"},
    )
    _timestamp_stream(
        second,
        first="2027-01-01T00:02:00Z",
        last="2027-01-01T00:03:00Z",
        input_tokens=150,
        output_tokens=15,
    )
    second_stored = crew.complete(second["run_id"], gate="passed")["record"]

    session_runs = [first_stored, second_stored]
    assert sum(run["budget"]["tokens"]["input_tokens"] for run in session_runs) == 150
    assert sum(run["budget"]["tokens"]["output_tokens"] for run in session_runs) == 15
    assert second_stored["budget"]["tokens"]["input_tokens"] == 50
    assert second_stored["budget"]["tokens"]["input_tokens_cumulative"] == 150


def test_session_cumulative_cost_is_labelled_and_differenced() -> None:
    first = ledger.per_run_budget({"cost_usd": 1.25})
    previous = {"budget": first}
    second = ledger.per_run_budget({"cost_usd": 2.0}, previous)

    assert first == {"cost_usd": 1.25, "cost_usd_cumulative": 1.25}
    assert second == {"cost_usd": 0.75, "cost_usd_cumulative": 2.0}


def test_promotion_uses_stream_mtime_for_untimestamped_stream(
    home, repo, monkeypatch
) -> None:
    record = _dispatch(repo, fixture="codex-turn.jsonl")
    stream_time = datetime(2027, 2, 3, 4, 5, 6, tzinfo=timezone.utc)
    os.utime(record["log_path"], (stream_time.timestamp(), stream_time.timestamp()))
    pointer = crew.read_pointer(record["run_id"])
    pointer["created_at"] = "2027-02-03T04:00:06Z"
    crew._write_json(crew.pointer_path(record["run_id"]), pointer)

    stored = crew.complete(record["run_id"], gate="passed")["record"]

    assert stored["completed_at"] == "2027-02-03T04:05:06Z"
    assert stored["completed_at_source"] == "stream_mtime"
    assert stored["worker_seconds"] == stored["wall_seconds"] == 300
    assert stored["worker_seconds_source"] == "wall_fallback"
    assert stored["stalled"] is False
    report = ledger.effort_report(PROJECT, root=repo, declared={"plan-a": "M"})
    assert report["plans"][0]["runs"] == 1
    assert report["plans"][0]["measured_minutes"] == 5.0


def test_stalled_untimestamped_stream_records_typed_duration_absence(
    home, repo
) -> None:
    record = _dispatch(repo, fixture="codex-turn.jsonl")
    stream_time = datetime(2027, 2, 3, 4, 5, 6, tzinfo=timezone.utc)
    os.utime(record["log_path"], (stream_time.timestamp(), stream_time.timestamp()))
    pointer = crew.read_pointer(record["run_id"])
    pointer["created_at"] = "2027-02-03T03:00:00Z"
    crew._write_json(crew.pointer_path(record["run_id"]), pointer)

    stored = crew.complete(record["run_id"], gate="passed")["record"]

    assert stored["worker_seconds"] is None
    assert stored["worker_seconds_source"] == "stalled"
    assert stored["wall_seconds"] == 3906
    assert stored["stalled"] is True
    report = ledger.effort_report(PROJECT, root=repo, declared={"plan-a": "M"})
    assert report["excluded_stalled"] == 1
    assert report["plans"][0]["runs"] == 0


def test_promotion_prefers_event_time_over_stream_mtime(home, repo) -> None:
    record = _dispatch(repo, fixture="codex-turn.jsonl")
    events = [
        json.loads(line) for line in Path(record["log_path"]).read_text().splitlines()
    ]
    events[-1]["timestamp"] = "2027-01-02T03:04:05Z"
    Path(record["log_path"]).write_text(
        "".join(json.dumps(event) + "\n" for event in events)
    )
    later = datetime(2028, 1, 1, tzinfo=timezone.utc).timestamp()
    os.utime(record["log_path"], (later, later))

    stored = crew.complete(record["run_id"], gate="passed")["record"]

    assert stored["completed_at"] == "2027-01-02T03:04:05Z"
    assert stored["completed_at_source"] == "terminal_event"


def test_promotion_uses_newest_resume_stream_mtime(home, repo) -> None:
    record = _dispatch(repo, fixture="codex-turn.jsonl")
    directory = Path(record["log_path"]).parent
    resume = directory / "resume-1.jsonl"
    resume.write_text(Path(record["log_path"]).read_text())
    first = datetime(2027, 1, 1, tzinfo=timezone.utc).timestamp()
    second = datetime(2027, 1, 2, tzinfo=timezone.utc).timestamp()
    os.utime(record["log_path"], (first, first))
    os.utime(resume, (second, second))

    stored = crew.complete(record["run_id"], gate="passed")["record"]

    assert stored["completed_at"] == "2027-01-02T00:00:00Z"
    assert stored["completed_at_source"] == "stream_mtime"


def test_resumed_promotion_keeps_the_original_stream_and_orders_turns(
    home, repo
) -> None:
    record = _dispatch(repo, fixture="codex-turn.jsonl")
    original = Path(record["log_path"])
    directory = original.parent
    streams = {
        1: directory / "resume-1.jsonl",
        2: directory / "resume-2.jsonl",
        10: directory / "resume-10.jsonl",
    }
    for turn, path in streams.items():
        path.write_text(original.read_text())
        instant = datetime(2027, 1, turn + 1, tzinfo=timezone.utc).timestamp()
        os.utime(path, (instant, instant))
    initial = datetime(2027, 1, 1, tzinfo=timezone.utc).timestamp()
    os.utime(original, (initial, initial))

    crew.record_resumption(
        record["run_id"],
        pid=999999999,
        turn=10,
        log_path=streams[10],
        stderr_path=directory / "resume-10.stderr.log",
    )
    pointer = crew.read_pointer(record["run_id"])
    crew_paths = crew._run_streams(original)
    ledger_paths = ledger._run_streams(record["run_id"], crew.crew_home() / "runs")
    stored = crew.complete(record["run_id"], gate="passed")["record"]

    expected = ["stream.jsonl", "resume-1.jsonl", "resume-2.jsonl", "resume-10.jsonl"]
    assert pointer["log_path"] == str(original)
    assert [path.name for path in crew_paths] == expected
    assert [path.name for path in ledger_paths] == expected
    assert stored["completed_at"] == "2027-01-11T00:00:00Z"
    assert stored["completed_at_source"] == "stream_mtime"


def test_promotion_uses_original_stream_when_it_has_newest_mtime(home, repo) -> None:
    record = _dispatch(repo, fixture="codex-turn.jsonl")
    directory = Path(record["log_path"]).parent
    resume = directory / "resume-1.jsonl"
    resume.write_text(Path(record["log_path"]).read_text())
    earlier = datetime(2027, 1, 1, tzinfo=timezone.utc).timestamp()
    later = datetime(2027, 1, 2, tzinfo=timezone.utc).timestamp()
    os.utime(resume, (earlier, earlier))
    os.utime(record["log_path"], (later, later))

    stored = crew.complete(record["run_id"], gate="passed")["record"]

    assert stored["completed_at"] == "2027-01-02T00:00:00Z"
    assert stored["completed_at_source"] == "stream_mtime"


def test_promotion_falls_back_when_no_stream_survives(home, repo, monkeypatch) -> None:
    record = _dispatch(repo)
    Path(record["log_path"]).unlink(missing_ok=True)
    monkeypatch.setattr(crew, "_utc_now", lambda: "2027-02-03T04:05:06Z")

    stored = crew.complete(record["run_id"], gate="passed")["record"]

    assert stored["completed_at"] == "2027-02-03T04:05:06Z"
    assert stored["completed_at_source"] == "promotion_time"


def test_every_completed_record_names_its_completion_time_source() -> None:
    stored = ledger.build_record(run_id="r-source", plan="plan-a", gate="passed")

    assert "completed_at_source" in ledger.RECORD_FIELDS
    assert "worker_seconds_source" in ledger.RECORD_FIELDS
    assert set(ledger.RECORD_FIELDS) <= set(stored)
    assert stored["completed_at_source"] == "promotion_time"
    assert stored["worker_seconds_source"] == "unavailable"


def test_a_completed_record_carries_the_declared_specification_level() -> None:
    stored = ledger.build_record(
        run_id="r-specified", plan="plan-a", gate="passed", spec_level="exact"
    )

    assert "spec_level" in ledger.RECORD_FIELDS
    assert stored["spec_level"] == "exact"


def test_completion_repair_reports_event_time_without_writing(home, repo) -> None:
    _historical_record(repo, "historical-event")
    _historical_stream(
        home,
        "historical-event",
        timestamp="2027-01-01T00:30:00Z",
        mtime="2027-01-01T01:00:00Z",
    )
    before, version_before = ledger.load(PROJECT, repo)

    report = ledger.repair_completion(PROJECT, root=repo)

    after, version_after = ledger.load(PROJECT, repo)
    assert report["write_requested"] is False
    assert report["written"] is False
    assert report["updated"] == 1
    assert report["rows"][0]["completion_source"] == "terminal_event"
    assert report["rows"][0]["completed_at"] == "2027-01-01T00:30:00Z"
    assert report["rows"][0]["worker_seconds"] == 1800
    assert after == before
    assert version_after == version_before


def test_completion_repair_writes_newest_resume_stream_mtime(home, repo) -> None:
    _historical_record(repo, "historical-resume")
    _historical_stream(home, "historical-resume", mtime="2027-01-01T00:20:00Z")
    _historical_stream(
        home,
        "historical-resume",
        mtime="2027-01-01T00:45:00Z",
        resume=True,
    )

    report = ledger.repair_completion(PROJECT, root=repo, write_changes=True)

    stored = ledger.runs(PROJECT, repo)[0]
    assert report["written"] is True
    assert report["updated"] == 1
    assert report["rows"][0]["completion_source"] == "stream_mtime"
    assert stored["completed_at"] == "2027-01-01T00:45:00Z"
    assert stored["completed_at_source"] == "stream_mtime"
    assert stored["worker_seconds"] == 2700


def test_completion_repair_leaves_missing_stream_record_unusable(home, repo) -> None:
    _historical_record(repo, "missing-stream")
    before, version_before = ledger.load(PROJECT, repo)

    report = ledger.repair_completion(PROJECT, root=repo, write_changes=True)

    after, version_after = ledger.load(PROJECT, repo)
    row = report["rows"][0]
    effort = ledger.effort_report(PROJECT, root=repo, declared={"plan-a": "M"})
    assert row["action"] == "unusable"
    assert row["calibration_usable"] is False
    assert row["completion_source"] is None
    assert "left unchanged" in row["detail"]
    assert report["written"] is False
    assert after == before
    assert version_after == version_before
    assert effort["excluded_unusable_completion"] == 0
    assert effort["plans"][0]["runs"] == 1


def test_completion_repair_reports_updated_unchanged_and_unusable_rows(
    home, repo
) -> None:
    _historical_record(repo, "needs-update")
    _historical_stream(home, "needs-update", timestamp="2027-01-01T00:30:00Z")
    _historical_record(
        repo,
        "already-derived",
        completed_at="2027-01-01T00:40:00Z",
        completed_at_source="terminal_event",
        worker_seconds=2400,
    )
    _historical_stream(home, "already-derived", timestamp="2027-01-01T00:40:00Z")
    _historical_record(repo, "gone")

    report = ledger.repair_completion(PROJECT, root=repo)

    rows = {row["run_id"]: row for row in report["rows"]}
    assert report["records"] == 3
    assert report["updated"] == 1
    assert report["unchanged"] == 1
    assert report["unusable"] == 1
    assert rows["needs-update"]["action"] == "updated"
    assert rows["needs-update"]["completion_source"] == "terminal_event"
    assert rows["already-derived"]["action"] == "unchanged"
    assert rows["already-derived"]["completion_source"] == "terminal_event"
    assert rows["gone"]["action"] == "unusable"


def test_completion_repair_write_is_idempotent(home, repo) -> None:
    _historical_record(repo, "idempotent")
    _historical_stream(home, "idempotent", mtime="2027-01-01T00:25:00Z")

    first = ledger.repair_completion(PROJECT, root=repo, write_changes=True)
    second = ledger.repair_completion(PROJECT, root=repo, write_changes=True)

    assert first["written"] is True
    assert first["version_after"] == first["version_before"] + 1
    assert second["written"] is False
    assert second["updated"] == 0
    assert second["unchanged"] == 1
    assert second["version_after"] == first["version_after"]


def test_completion_repair_command_requires_write_flag_to_persist(home, repo) -> None:
    _historical_record(repo, "command-record")
    _historical_stream(home, "command-record", timestamp="2027-01-01T00:15:00Z")
    _, version_before = ledger.load(PROJECT, repo)

    preview = CliRunner().invoke(
        cli_main,
        [
            "crew",
            "repair-completion",
            "--project",
            PROJECT,
            "--checkout-path",
            str(repo),
        ],
    )
    _, version_after_preview = ledger.load(PROJECT, repo)
    applied = CliRunner().invoke(
        cli_main,
        [
            "crew",
            "repair-completion",
            "--project",
            PROJECT,
            "--checkout-path",
            str(repo),
            "--write",
        ],
    )

    preview_payload = json.loads(preview.output)
    applied_payload = json.loads(applied.output)
    assert preview.exit_code == applied.exit_code == 0
    assert preview_payload["write_requested"] is False
    assert preview_payload["written"] is False
    assert version_after_preview == version_before
    assert applied_payload["write_requested"] is True
    assert applied_payload["written"] is True
    assert ledger.runs(PROJECT, repo)[0]["completed_at"] == "2027-01-01T00:15:00Z"


def test_record_reads_filter_by_target_time_and_count(home, repo) -> None:
    from reckon import mcp

    for run_id, plan, completed_at in (
        ("run-early", "alpha", "2027-01-01T01:00:00Z"),
        ("run-other", "beta", "2027-01-01T03:00:00Z"),
        ("run-middle", "alpha", "2027-01-01T04:00:00Z"),
        ("run-latest", "alpha", "2027-01-01T05:00:00Z"),
    ):
        ledger.append_run(
            PROJECT,
            ledger.build_record(
                run_id=run_id,
                plan=plan,
                gate="passed",
                completed_at=completed_at,
            ),
            root=repo,
        )

    selected = ledger.runs(
        PROJECT,
        repo,
        plan="alpha",
        since="2027-01-01T02:00:00Z",
        limit=1,
    )
    exposed = mcp._crew(
        PROJECT,
        view="records",
        checkout_path=str(repo),
        plan="alpha",
        since="2027-01-01T02:00:00Z",
        limit=1,
    )

    assert [record["run_id"] for record in selected] == ["run-latest"]
    assert [record["run_id"] for record in exposed["runs"]] == ["run-latest"]
    assert exposed["version"] == 4
    assert "members" not in exposed
    assert "holds" not in exposed


def test_an_unknown_gate_verdict_is_refused(home, repo) -> None:
    record = _dispatch(repo)
    with pytest.raises(ledger.LedgerError) as excinfo:
        crew.complete(record["run_id"], gate="looks fine")
    assert "not-run" in str(excinfo.value)
    assert crew.pointer_path(record["run_id"]).exists()


def test_complete_command_assumes_utc_for_a_naive_completion_stamp(home, repo) -> None:
    record = _dispatch(repo)

    result = CliRunner().invoke(
        cli_main,
        [
            "crew",
            "complete",
            "--run",
            record["run_id"],
            "--gate",
            "passed",
            "--completed-at",
            "2027-01-01T02:00:00",
        ],
    )

    payload = json.loads(result.output)
    stored = payload["record"]
    assert result.exit_code == 0
    assert stored["completed_at"] == "2027-01-01T02:00:00Z"
    assert stored["worker_seconds"] is None
    assert stored["worker_seconds_source"] == "unavailable"
    assert stored["wall_seconds"] is not None
    assert ledger._worker_seconds("2027-01-01T01:00:00", "2027-01-01T02:00:00") == 3600


def test_explicit_promotion_stamp_reaches_every_duration_consumer(home, repo) -> None:
    record = _dispatch(repo, fixture="codex-turn.jsonl")
    pointer = crew.read_pointer(record["run_id"])
    pointer["created_at"] = "2027-01-01T01:45:00Z"
    crew._write_json(crew.pointer_path(record["run_id"]), pointer)
    plan = repo / "docs" / "plans" / "plan-a.html"
    plan.parent.mkdir(parents=True)
    plan.write_text(
        '<meta name="docs-project" content="proj">'
        '<meta name="reckon-type" content="plan">'
        '<meta name="plan-slug" content="plan-a">'
        '<meta name="plan-effort-hours" content="1.0">'
    )

    stored = crew.complete(
        record["run_id"],
        gate="passed",
        completed_at="2027-01-01T02:00:00Z",
    )["record"]
    effort = ledger.effort_report(PROJECT, root=repo, declared={"plan-a": "M"})
    derived = capabilities.derive_capabilities({PROJECT: repo / "docs"})
    observation = calibration._observation(stored)

    assert stored["completed_at_source"] == "provided"
    assert stored["worker_seconds"] == 900
    assert effort["plans"][0]["runs"] == 1
    assert effort["excluded"] == {
        "scope_changed": 0,
        "stalled": 0,
        "unusable_completion": 0,
    }
    assert derived["configurations"][0]["runs"] == 1
    assert derived["excluded"]["unusable_completion"] == 0
    assert observation == ("plan-a", calibration.agent_configuration_key(stored), 0.25)


# ── Nothing transient is committed ──────────────────────────────────────────


def test_no_live_pointer_path_resolves_inside_a_working_tree(home, repo) -> None:
    """The pointer churns every few seconds; committing it would be noise."""
    record = _dispatch(repo)
    for path in (
        crew.crew_home(),
        crew.live_dir(),
        crew.pointer_path(record["run_id"]),
    ):
        assert not path.resolve().is_relative_to(repo.resolve())
    assert not Path(record["log_path"]).resolve().is_relative_to(repo.resolve())


def test_a_run_in_flight_leaves_the_working_tree_clean(home, repo) -> None:
    ledger.register_member(PROJECT, "worker-a", harness="alpha", root=repo)
    subprocess.run(
        ["git", "add", f"docs/state/{PROJECT}/crew.json"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", "test: seed member\n\nFixture state."],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    record = _dispatch(repo, member="worker-a")
    assert _porcelain(repo) == []
    # The worker's own scoped files live in its worktree, not the main tree.
    assert Path(record["worktree"]).is_dir()
    assert not Path(record["worktree"]).resolve().is_relative_to(repo.resolve())


# ── Interruption is recoverable ─────────────────────────────────────────────


def test_a_killed_run_that_delivered_is_completed_but_unpromoted(home, repo) -> None:
    record = _dispatch(repo, fixture="codex-turn.jsonl")
    _deliver(record)
    _kill(record)

    report = crew.recover(project=PROJECT)
    row = report["runs"][0]

    assert row["classification"] == "completed_unpromoted"
    assert row["manifest_path"] == record["manifest_path"]
    assert row["next_action"].startswith(
        f"reckon crew complete --run {record['run_id']}"
    )
    # And promotion then succeeds, so nothing was lost by the interruption.
    promoted = crew.complete(
        record["run_id"], gate="passed", commits=[record["base_sha"]]
    )
    assert promoted["record"]["run_id"] == record["run_id"]


def test_a_dead_run_that_delivered_nothing_is_abandoned(home, repo) -> None:
    record = _dispatch(repo)
    _kill(record)

    row = crew.recover()["runs"][0]

    assert row["classification"] == "abandoned"
    assert "nothing" in row["detail"]
    assert record["stderr_path"] in row["next_action"]


def test_a_live_process_is_running(home, repo) -> None:
    record = _dispatch(repo)
    row = crew.recover()["runs"][0]
    assert row["classification"] == "running"
    assert row["process_alive"] is True
    assert row["next_action"] == f"reckon crew observe --run {record['run_id']}"


def test_recovery_reports_all_three_classes_and_counts_them(home, repo) -> None:
    running = _dispatch(
        repo,
        node_kwargs={"id": "node-live", "write_paths": ["reckon/live.py"]},
    )
    delivered = _dispatch(
        repo,
        fixture="codex-turn.jsonl",
        node_kwargs={"id": "node-done", "write_paths": ["reckon/delivered.py"]},
    )
    _deliver(delivered)
    dead = _dispatch(
        repo,
        node_kwargs={"id": "node-dead", "write_paths": ["reckon/dead.py"]},
    )
    _kill(dead)

    report = crew.recover()

    assert report["counts"] == {
        "running": 1,
        "completed_unpromoted": 1,
        "abandoned": 1,
    }
    assert {row["run_id"] for row in report["runs"]} == {
        running["run_id"],
        delivered["run_id"],
        dead["run_id"],
    }


def test_derived_member_guard_ignores_a_run_from_another_repository(
    home, repo
) -> None:
    session = "shared-coordinator-session"
    member = crew._session_member_id(session)
    ledger.register_member(PROJECT, member, harness="alpha", root=repo)
    foreign_run = {
        "run_id": "r-foreign",
        "project": PROJECT,
        "repo": str(home.parent / "outside-repository"),
        "member": member,
        "phase": "running",
    }
    crew._write_json(crew.pointer_path(foreign_run["run_id"]), foreign_run)

    record = _dispatch(repo, session=session, node_kwargs={"id": "node-local"})

    assert record["member"] == member
    assert {pointer["run_id"] for pointer in crew.list_live()} == {
        foreign_run["run_id"],
        record["run_id"],
    }


def test_recovery_never_removes_a_worktree(home, repo) -> None:
    """A refused removal is a visible blocker, not something to force."""
    record = _dispatch(repo)
    _kill(record)
    crew.recover()
    assert Path(record["worktree"]).is_dir()
    assert crew.pointer_path(record["run_id"]).exists()


# ── Concurrent writes are safe ──────────────────────────────────────────────


def test_a_stale_expected_version_is_refused(home, repo) -> None:
    ledger.register_member(PROJECT, "worker-a", harness="alpha", root=repo)
    data, version = ledger.load(PROJECT, repo)
    ledger.register_member(PROJECT, "worker-b", harness="alpha", root=repo)

    with pytest.raises(ledger.LedgerError) as excinfo:
        ledger.write(PROJECT, data, version, repo)

    assert "re-read and retry" in str(excinfo.value)


def test_two_interleaved_promotions_both_survive(home, repo, monkeypatch) -> None:
    """The loser re-reads the winner's ledger and appends to that."""
    first = ledger.build_record(run_id="r-one", plan="plan-a", gate="passed")
    ledger.append_run(PROJECT, first, root=repo)

    real_write = _store._write_json_envelope
    intruder = ledger.build_record(run_id="r-two", plan="plan-a", gate="passed")
    calls: list[int] = []

    def racing_write(path, project, slug, data, expected_version):
        calls.append(expected_version)
        if len(calls) == 1:
            # A concurrent orchestrator lands its record first.
            competing, version = ledger.load(project, repo)
            competing["runs"].append(dict(intruder))
            real_write(path, project, slug, competing, version)
        return real_write(path, project, slug, data, expected_version)

    monkeypatch.setattr(_store, "_write_json_envelope", racing_write)
    third = ledger.build_record(run_id="r-three", plan="plan-a", gate="passed")
    ledger.append_run(PROJECT, third, root=repo)

    assert [item["run_id"] for item in ledger.runs(PROJECT, repo)] == [
        "r-one",
        "r-two",
        "r-three",
    ]
    assert len(calls) > 1, "the interleaved write must have forced a retry"


def test_promotion_refuses_a_merge_conflicted_ledger(home, repo) -> None:
    record = _dispatch(repo)
    _deliver(record)
    path = ledger.ledger_path(PROJECT, repo)
    conflicted = b'<<<<<<< HEAD\n{"data": {"runs": []}}\n=======\n{}\n>>>>>>> branch\n'
    path.write_bytes(conflicted)

    with pytest.raises(_store.CorruptEnvelopeError) as excinfo:
        crew.complete(record["run_id"], gate="passed", root=repo)

    message = str(excinfo.value)
    assert str(path) in message
    assert "fix any conflict markers" in message
    assert "restore the file from git" in message
    assert path.read_bytes() == conflicted
    assert crew.pointer_path(record["run_id"]).exists()


def test_hold_check_backs_off_after_a_version_conflict(home, repo, monkeypatch) -> None:
    real_write = ledger.write
    writes = 0
    backoffs: list[int] = []

    def conflicting_write(project, data, expected_version, root=None):
        nonlocal writes
        writes += 1
        if writes == 1:
            raise ledger.LedgerError("concurrent write")
        return real_write(project, data, expected_version, root)

    monkeypatch.setattr(ledger, "write", conflicting_write)
    monkeypatch.setattr(ledger, "_retry_backoff", backoffs.append)

    result = ledger.record_hold_checks(
        PROJECT,
        [{"backend": "alpha", "held": True}],
        checked_at="2026-08-16T20:00:00Z",
        root=repo,
    )

    assert result["version"] == 1
    assert backoffs == [0]


def test_promoting_the_same_run_twice_is_refused(home, repo) -> None:
    """A double promotion double-counts every measurement it carries."""
    record = ledger.build_record(run_id="r-one", plan="plan-a", gate="passed")
    ledger.append_run(PROJECT, record, root=repo)

    with pytest.raises(ledger.LedgerError) as excinfo:
        ledger.append_run(PROJECT, record, root=repo)

    assert "double-count" in str(excinfo.value)
    assert len(ledger.runs(PROJECT, repo)) == 1


# ── Session reuse through the roster ────────────────────────────────────────


def test_a_member_with_a_null_session_captures_one_from_its_first_run(
    home, repo
) -> None:
    ledger.register_member(PROJECT, "worker-a", harness="alpha", root=repo)
    assert ledger.member(PROJECT, "worker-a", repo)["session_id"] is None

    record = _dispatch(repo, fixture="codex-turn.jsonl", member="worker-a")
    observed = crew.observe(record["run_id"])

    assert observed["session_capture"]["captured"] is True
    assert ledger.member(PROJECT, "worker-a", repo)["session_id"] == SESSION_ID


def test_a_second_node_reaches_the_members_captured_session(home, repo) -> None:
    ledger.register_member(PROJECT, "worker-a", harness="alpha", root=repo)
    first = _dispatch(repo, fixture="codex-turn.jsonl", member="worker-a")
    crew.observe(first["run_id"])

    second = _dispatch(
        repo,
        member="worker-a",
        node_kwargs={"id": "node-b", "write_paths": ["reckon/session.py"]},
    )

    assert second["session_id"] == SESSION_ID
    resumed = second["argv"][second["argv"].index("resume") + 1]
    assert resumed == SESSION_ID


def test_a_later_session_is_not_written_over_the_captured_one(home, repo) -> None:
    """Overwriting would silently retire the long-lived session."""
    ledger.register_member(
        PROJECT, "worker-a", harness="alpha", session_id="first-session", root=repo
    )
    capture = ledger.capture_session(PROJECT, "worker-a", "second-session", repo)

    assert capture["captured"] is False
    assert "not written over the top" in capture["detail"]
    assert ledger.member(PROJECT, "worker-a", repo)["session_id"] == "first-session"


def test_dispatching_to_an_unregistered_member_is_refused(home, repo) -> None:
    with pytest.raises(crew.CrewError) as excinfo:
        _dispatch(repo, member="ghost")
    assert "reckon crew member add" in str(excinfo.value)
    assert crew.list_live() == []


def test_a_promoted_record_names_the_member_and_its_session(home, repo) -> None:
    ledger.register_member(PROJECT, "worker-a", harness="alpha", root=repo)
    record = _dispatch(repo, fixture="codex-turn.jsonl", member="worker-a")
    crew.observe(record["run_id"])
    _deliver(record)

    promoted = crew.complete(
        record["run_id"], gate="passed", commits=[record["base_sha"]]
    )

    assert promoted["record"]["member"] == "worker-a"
    assert promoted["record"]["session_id"] == SESSION_ID


# ── Calibration inputs ──────────────────────────────────────────────────────


def test_a_completed_record_carries_every_calibration_input(home, repo) -> None:
    record = _dispatch(repo, fixture="codex-turn.jsonl")
    _deliver(record)

    stored = crew.complete(
        record["run_id"],
        gate="passed",
        commits=[record["base_sha"]],
        tests_added=9,
        outcome="landed the dispatch primitive",
    )["record"]

    assert set(ledger.RECORD_FIELDS) <= set(stored)
    assert stored["agent"] == {
        "backend": "alpha",
        "launch": "cli",
        "model": "some-model",
        "effort": "high",
        "sandbox": "worktree-full",
    }
    assert stored["dispatched_at"] and stored["completed_at"]
    assert stored["worker_seconds"] == stored["wall_seconds"]
    assert stored["worker_seconds_source"] == "wall_fallback"
    assert stored["wall_seconds"] is not None
    assert stored["stalled"] is False
    assert stored["time_budget"] == "20m"
    assert stored["tests_added"] == 9
    assert stored["gate"] == "passed"
    assert stored["base_sha"]
    # Whatever headroom the backend reported travels with the record: the pointer
    # that held it is gone, and a later pre-flight reading a call instead would
    # spend the resource it is measuring.
    assert stored["budget"]["headroom"] in ("known", "unknown")


def test_the_scope_changed_flag_defaults_false_and_is_settable(home, repo) -> None:
    honest = _dispatch(
        repo,
        node_kwargs={"id": "node-honest", "write_paths": ["reckon/honest.py"]},
    )
    widened = _dispatch(
        repo,
        node_kwargs={"id": "node-widened", "write_paths": ["reckon/widened.py"]},
    )

    assert (
        crew.complete(honest["run_id"], gate="passed")["record"]["scope_changed"]
        is False
    )
    assert (
        crew.complete(widened["run_id"], gate="passed", scope_changed=True)["record"][
            "scope_changed"
        ]
        is True
    )


def test_changed_lines_are_measured_from_the_scoped_diff(home, repo) -> None:
    """A count over the whole diff would describe the branch, not the node."""
    record = _dispatch(repo)
    worktree = Path(record["worktree"])
    (worktree / "reckon" / "target.py").write_text("value = 1\nvalue2 = 2\n")
    (worktree / "other.py").write_text("outside = 1\noutside2 = 2\n")
    for args in (
        ["add", "reckon/target.py", "other.py"],
        ["commit", "-q", "-m", "feat: widen the target\n\nBody."],
    ):
        subprocess.run(["git", *args], cwd=worktree, check=True, capture_output=True)
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=worktree,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    stored = crew.complete(
        record["run_id"],
        gate="passed",
        commits=[commit],
        changed_lines={"detail": "not a measurement"},
    )["record"]

    assert stored["changed_lines"] == {"added": 1, "removed": 0, "files": 1}


def test_measured_worker_time_is_reported_against_declared_effort(home, repo) -> None:
    for index, seconds in enumerate((600, 1800)):
        ledger.append_run(
            PROJECT,
            ledger.build_record(
                run_id=f"r-{index}",
                plan="plan-a",
                gate="passed",
                worker_seconds=seconds,
                completed_at_source="terminal_event",
            ),
            root=repo,
        )

    report = ledger.effort_report(PROJECT, root=repo, declared={"plan-a": "M"})
    row = report["plans"][0]

    assert row["declared_effort"] == "M"
    assert row["runs"] == 2
    assert row["measured_minutes"] == 40.0
    assert row["mean_minutes"] == 20.0
    assert (row["min_minutes"], row["max_minutes"]) == (10.0, 30.0)
    assert row["spread_minutes"] == 20.0
    assert report["by_effort"][0] == {
        "effort": "M",
        "plans": 1,
        "runs": 2,
        "mean_minutes": 20.0,
        "spread_minutes": 20.0,
    }


def test_a_scope_changed_run_is_excluded_from_the_measured_columns(home, repo) -> None:
    """It measures neither the estimate nor the worker."""
    ledger.append_run(
        PROJECT,
        ledger.build_record(
            run_id="r-honest",
            plan="plan-a",
            gate="passed",
            worker_seconds=600,
            completed_at_source="terminal_event",
        ),
        root=repo,
    )
    ledger.append_run(
        PROJECT,
        ledger.build_record(
            run_id="r-widened",
            plan="plan-a",
            gate="passed",
            worker_seconds=6000,
            scope_changed=True,
            completed_at_source="terminal_event",
        ),
        root=repo,
    )

    report = ledger.effort_report(PROJECT, root=repo, declared={"plan-a": "M"})

    assert report["excluded_scope_changed"] == 1
    assert report["plans"][0]["runs"] == 1
    assert report["plans"][0]["mean_minutes"] == 10.0
    assert report["plans"][0]["excluded_scope_changed"] == 1


def test_promotion_time_is_reported_as_unusable_for_calibration(home, repo) -> None:
    ledger.append_run(
        PROJECT,
        ledger.build_record(
            run_id="r-stream",
            plan="plan-a",
            gate="passed",
            worker_seconds=600,
            completed_at_source="stream_mtime",
        ),
        root=repo,
    )
    ledger.append_run(
        PROJECT,
        ledger.build_record(
            run_id="r-promotion",
            plan="plan-a",
            gate="passed",
            worker_seconds=6000,
            completed_at_source="promotion_time",
        ),
        root=repo,
    )

    report = ledger.effort_report(PROJECT, root=repo, declared={"plan-a": "M"})
    row = report["plans"][0]

    assert report["excluded_unusable_completion"] == 1
    assert row["excluded_unusable_completion"] == 1
    assert row["runs"] == 1
    assert row["mean_minutes"] == 10.0


def test_explicit_completion_time_is_usable_for_effort(home, repo) -> None:
    ledger.append_run(
        PROJECT,
        ledger.build_record(
            run_id="r-explicit",
            plan="plan-a",
            gate="passed",
            worker_seconds=900,
            completed_at_source="provided",
        ),
        root=repo,
    )

    report = ledger.effort_report(PROJECT, root=repo, declared={"plan-a": "M"})

    assert report["plans"][0]["runs"] == 1
    assert report["plans"][0]["measured_minutes"] == 15.0
    assert report["excluded_unusable_completion"] == 0


def test_declared_effort_is_read_from_the_plans_themselves(home, repo) -> None:
    """One copy of the claim, so it cannot drift from the measurement."""
    plan = repo / "docs" / "plans" / "plan-a.html"
    plan.parent.mkdir(parents=True)
    plan.write_text(
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f'<meta name="docs-project" content="{PROJECT}">'
        '<meta name="reckon-type" content="plan">'
        '<meta name="plan-slug" content="plan-a">'
        '<meta name="plan-effort" content="L">'
        "<title>Plan A</title></head><body></body></html>"
    )
    assert ledger.declared_efforts(PROJECT, repo) == {"plan-a": "L"}


def test_the_summary_rolls_up_gates_roster_and_measured_effort(home, repo) -> None:
    ledger.register_member(PROJECT, "worker-a", harness="alpha", root=repo)
    ledger.append_run(
        PROJECT,
        ledger.build_record(
            run_id="r-pass",
            plan="plan-a",
            gate="passed",
            worker_seconds=600,
            completed_at_source="terminal_event",
        ),
        root=repo,
    )
    ledger.append_run(
        PROJECT,
        ledger.build_record(run_id="r-fail", plan="plan-b", gate="failed"),
        root=repo,
    )

    summary = ledger.summary(PROJECT, root=repo)

    assert summary["runs"] == 2
    assert summary["gates"] == {"failed": 1, "passed": 1}
    assert summary["members"] == 1
    assert summary["members_with_session"] == 0
    assert summary["plans"] == ["plan-a", "plan-b"]
    assert summary["effort"]["plans"][0]["plan"] == "plan-a"


# ── The read tool ───────────────────────────────────────────────────────────


def test_hold_history_is_exposed_with_count_and_total_duration(home, repo) -> None:
    held = {
        "backend": "alpha",
        "purpose": "dispatch",
        "held": True,
        "effective_ceiling_pct": 95.0,
        "reason": "utilisation is at the effective ceiling",
        "state": {
            "utilisation_pct": 97.0,
            "resets_at": "2026-08-12T12:10:00Z",
        },
    }
    clear = {**held, "purpose": "resume", "held": False}
    ledger.record_hold_checks(
        PROJECT, [held], checked_at="2026-08-12T12:00:00Z", root=repo
    )
    ledger.record_hold_checks(
        PROJECT, [clear], checked_at="2026-08-12T12:02:15Z", root=repo
    )

    summary = ledger.summary(PROJECT, root=repo)
    records = json.loads(
        CliRunner()
        .invoke(
            cli_main,
            [
                "crew",
                "ledger",
                "--project",
                PROJECT,
                "--view",
                "records",
                "--checkout-path",
                str(repo),
            ],
        )
        .output
    )
    from reckon import mcp

    committed = mcp._crew(PROJECT, view="ledger", checkout_path=str(repo))
    assert summary["holds"] == 1
    assert summary["open_holds"] == 0
    assert summary["total_held_seconds"] == 135
    assert records["holds"][0]["held_seconds"] == 135
    assert committed["holds"][0]["hold_id"] == records["holds"][0]["hold_id"]


def _worktree(repo: Path, name: str) -> Path:
    """Cut a second checkout of the repo, as a worker would work inside."""
    path = repo.parent / name
    subprocess.run(
        ["git", "worktree", "add", "--detach", str(path), "HEAD"],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )
    return path


def test_the_crew_tool_reads_the_ledger_and_the_live_pointers(home, repo) -> None:
    from reckon import mcp

    record = _dispatch(repo)
    ledger.append_run(
        PROJECT,
        ledger.build_record(run_id="r-one", plan="plan-a", gate="passed"),
        root=repo,
    )

    committed = mcp._crew(PROJECT, view="ledger", checkout_path=str(repo))
    live = mcp._crew(PROJECT, view="live", checkout_path=str(repo))

    assert [item["run_id"] for item in committed["runs"]] == ["r-one"]
    assert committed["version"] == 2
    assert [row["run_id"] for row in live["runs"]] == [record["run_id"]]
    assert live["runs"][0]["classification"] == "running"


def test_the_crew_tool_reads_the_worktrees_ledger_not_the_main_one(home, repo) -> None:
    """A worker inside a worktree must not read the registered checkout."""
    from reckon import mcp

    worktree = _worktree(repo, "worker-checkout")
    ledger.append_run(
        PROJECT,
        ledger.build_record(run_id="r-main", plan="plan-a", gate="passed"),
        root=repo,
    )
    ledger.append_run(
        PROJECT,
        ledger.build_record(run_id="r-worktree", plan="plan-a", gate="passed"),
        root=worktree,
    )

    from_worktree = mcp._crew(PROJECT, view="ledger", checkout_path=str(worktree))
    from_main = mcp._crew(PROJECT, view="ledger", checkout_path=str(repo))

    assert [item["run_id"] for item in from_worktree["runs"]] == ["r-worktree"]
    assert [item["run_id"] for item in from_main["runs"]] == ["r-main"]
    assert from_worktree["path"].startswith(str(worktree))


def test_an_unknown_crew_view_names_every_view_it_has(home, repo) -> None:
    from reckon import mcp

    result = mcp._crew(PROJECT, view="everything")

    assert result["ok"] is False
    assert "summary, flight, live, records, ledger or budget" in result["detail"]


def test_the_crew_tool_reads_budget_headroom_from_the_ledger(home, repo) -> None:
    """The pre-flight an in-harness orchestrator reads is the same one the CLI runs."""
    from reckon import mcp

    ledger.append_run(
        PROJECT,
        ledger.build_record(
            run_id="r-one",
            plan="plan-a",
            gate="passed",
            agent={"backend": "native"},
            budget={
                "headroom": "known",
                "utilisation_pct": 100.0,
                "resets_at": "2099-01-01T00:00:00Z",
            },
        ),
        root=repo,
    )

    report = mcp._crew(PROJECT, view="budget", checkout_path=str(repo))

    assert report["ok"] is True
    assert report["held"] is True
    assert report["held_backends"] == ["native"]
    assert report["resume_at"] == "2099-01-01T00:00:00Z"


def test_the_crew_budget_view_never_records_a_hold(home, repo) -> None:
    from reckon import mcp

    ledger.append_run(
        PROJECT,
        ledger.build_record(
            run_id="r-one",
            plan="plan-a",
            gate="passed",
            agent={"backend": "native"},
            budget={
                "headroom": "known",
                "utilisation_pct": 100.0,
                "resets_at": "2099-01-01T00:00:00Z",
            },
        ),
        root=repo,
    )
    before, version_before = ledger.load(PROJECT, repo)

    report = mcp._crew(PROJECT, view="budget", checkout_path=str(repo))

    after, version_after = ledger.load(PROJECT, repo)
    assert report["held"] is True
    assert version_after == version_before
    assert after["holds"] == before["holds"] == []


def test_the_mcp_surface_holds_at_five_tools() -> None:
    from reckon import mcp

    names = {item.name for item in mcp.mcp._tool_manager.list_tools()}

    assert names == {"_read_plan", "_edit_plan", "_roadmap", "_audit", "_crew"}
