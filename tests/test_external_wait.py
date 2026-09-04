"""External conditions remain visible waits and resume when they terminate."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reckon.crew import recovery
from reckon.crew.resumption import sweep
from reckon.crew.runs import _write_json, pointer_path

PROJECT = "proj"
DEAD_PID = 4_194_303


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home


def _waiting_run(
    tmp_path: Path,
    run_id: str,
    *,
    started_at: str | None = None,
) -> dict:
    started_at = started_at or datetime.now(tz=UTC).isoformat().replace("+00:00", "Z")
    directory = tmp_path / "runs" / run_id
    directory.mkdir(parents=True, exist_ok=True)
    manifest = directory / "manifest.md"
    manifest.write_text(
        "\n".join(
            (
                f"node: {run_id}",
                "status: waiting",
                "wait_condition: cluster job 7788",
                'wait_probe: ["scheduler-status", "--job", "7788"]',
                "wait_terminal: COMPLETED, FAILED, CANCELLED",
                f"wait_started_at: {started_at}",
                "resume_brief: cluster job 7788 finished; inspect its result and finish the report",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    tree = tmp_path / "trees" / run_id
    tree.mkdir(parents=True)
    record = {
        "run_id": run_id,
        "project": PROJECT,
        "repo": str(tmp_path / "repo"),
        "worktree": str(tree),
        "launch": "cli",
        "argv": ["codex", "exec"],
        "backend": "alpha",
        "role": "implement",
        "session_id": f"session-{run_id}",
        "created_at": started_at,
        "log_path": str(directory / "stream.jsonl"),
        "stderr_path": str(directory / "stderr.log"),
        "manifest_path": str(manifest),
        "phase": "complete",
        "process_alive": False,
        "node": {
            "id": run_id,
            "plan": "plan-a",
            "section": "external-condition",
            "time_budget": "30m",
            "write_paths": ["reckon/one.py"],
        },
    }
    _write_json(pointer_path(run_id), record)
    return record


class _Launcher:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, plan, *, log_path, stderr_path, prompt_path) -> int:
        self.calls.append(
            {
                "prompt": Path(prompt_path).read_text(encoding="utf-8").strip(),
                "log_path": Path(log_path),
                "plan": plan,
            }
        )
        return DEAD_PID


def test_four_waits_render_in_the_followers_own_bucket(
    home: Path, tmp_path: Path
) -> None:
    for index in range(4):
        _waiting_run(tmp_path, f"r-wait-{index}")

    events = recovery.watch_ticker(PROJECT, poll_interval=0)
    try:
        baselines = [next(events) for _ in range(4)]
    finally:
        events.close()

    assert {event["to_state"] for event in baselines} == {"waiting"}
    assert {event["waiting"] for event in baselines} == {4}
    assert {event["blocked"] for event in baselines} == {0}
    assert {event["working"] for event in baselines} == {0}
    rendered = [recovery.format_watch_transition(event) for event in baselines]
    assert all("waiting" in line and " 4q" in line for line in rendered)


def test_the_sweep_resumes_only_after_the_condition_terminates(
    home: Path, tmp_path: Path
) -> None:
    run_id = "r-waiting-job"
    _waiting_run(tmp_path, run_id)
    launcher = _Launcher()
    observations = iter(
        (
            {"terminal": False, "observed": "RUNNING"},
            {"terminal": True, "observed": "COMPLETED"},
            {"terminal": True, "observed": "COMPLETED"},
        )
    )
    checked: list[str] = []

    def condition_test(pointer, wait):
        checked.append(wait["condition"])
        return next(observations)

    pending = sweep(PROJECT, launcher=launcher, condition_test=condition_test)
    assert pending["resumed"] == []
    assert pending["skipped"][0]["reason"] == "condition-pending"
    assert launcher.calls == []

    finished = sweep(PROJECT, launcher=launcher, condition_test=condition_test)
    assert [item["run_id"] for item in finished["resumed"]] == [run_id]
    assert finished["resumed"][0]["observed"] == "COMPLETED"
    assert launcher.calls[0]["prompt"] == (
        "cluster job 7788 finished; inspect its result and finish the report"
    )

    repeated = sweep(PROJECT, launcher=launcher, condition_test=condition_test)
    assert repeated["resumed"] == []
    assert repeated["skipped"] == []
    assert len(launcher.calls) == 1
    assert checked == ["cluster job 7788"] * 2


def test_an_old_wait_surfaces_its_age_without_becoming_blocked(
    home: Path, tmp_path: Path
) -> None:
    started = datetime(2026, 9, 4, 4, 0, tzinfo=UTC)
    pointer = _waiting_run(
        tmp_path,
        "r-old-wait",
        started_at=started.isoformat().replace("+00:00", "Z"),
    )

    snapshot = recovery._watch_snapshot(
        pointer,
        moment=(started + timedelta(hours=2)).timestamp(),
        stall_seconds=3600,
    )
    counts = recovery._fleet_counts({pointer["run_id"]: snapshot})
    event = recovery._watch_transition(
        PROJECT,
        kind="transition",
        snapshot=snapshot,
        previous="waiting",
        current=snapshot["state"],
        counts=counts,
    )
    line = recovery.format_watch_transition(event)

    assert snapshot["state"] == "wait-aged"
    assert counts == {"working": 0, "blocked": 0, "unpromoted": 0, "waiting": 1}
    assert event["blocked"] == 0 and event["waiting"] == 1
    assert "7200s" in snapshot["detail"]
    assert "!" in line and "wait-aged" in line


def test_a_crash_without_a_wait_declaration_stays_abandoned(
    home: Path, tmp_path: Path
) -> None:
    pointer = _waiting_run(tmp_path, "r-crashed")
    Path(pointer["manifest_path"]).unlink()

    row = recovery.classify_pointer(pointer)
    snapshot = recovery._watch_snapshot(
        pointer,
        moment=datetime.now(tz=UTC).timestamp(),
        stall_seconds=3600,
    )
    counts = recovery._fleet_counts({pointer["run_id"]: snapshot})

    assert row["classification"] == "abandoned"
    assert snapshot["state"] == "abandoned"
    assert counts == {"working": 0, "blocked": 1, "unpromoted": 0}
    assert "waiting" not in counts
