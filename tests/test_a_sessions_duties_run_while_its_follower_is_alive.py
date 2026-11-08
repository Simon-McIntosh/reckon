"""A session replays a due sweep after a running sweep has read its duties."""

from __future__ import annotations

import importlib
import json
import os
import socket
import subprocess
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reckon.crew import resumption, review, runs, station, station_store

PROJECT = "session-duties-fixture"
SESSION = "coordinator-session"
MOMENT = datetime(2026, 11, 7, 18, 10, tzinfo=UTC)
CONFIG = {
    "default_backend": "alpha",
    "local_backend": "alpha",
    "backends": {
        "alpha": {
            "launch": "cli",
            "command": "codex",
            "model": "some-model",
            "effort": "high",
            "sandbox": "worktree-full",
            "session_reuse": True,
            "time_budget": "25m",
        }
    },
    "roles": {"implement": {}, "review": {}},
    "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def _crew(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    plans = repo / "docs" / "plans"
    plans.mkdir(parents=True)
    (plans / "fixture.html").write_text(
        f'<meta name="docs-project" content="{PROJECT}">'
        '<meta name="reckon-type" content="plan">'
        '<meta name="plan-slug" content="fixture">'
        '<h2 id="s2">A session dispatches its duties</h2>',
        encoding="utf-8",
    )
    (repo / "module.py").write_text("value = 1\n", encoding="utf-8")
    for args in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "module.py", "docs/plans/fixture.html"),
        ("commit", "-q", "-m", "chore: seed"),
    ):
        _git(repo, *args)
    (repo / "module.py").write_text("value = 2\n", encoding="utf-8")
    _git(repo, "add", "module.py")
    _git(repo, "commit", "-q", "-m", "fix: update value")
    head = _git(repo, "rev-parse", "HEAD")
    (home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repo / "docs")}), encoding="utf-8"
    )
    dispatcher = importlib.import_module("reckon.crew.dispatch")

    def prepare(_repo: Path, session: str, node: str, base: str) -> dict:
        path = tmp_path / "worktrees" / f"{session}-{node}"
        path.mkdir(parents=True, exist_ok=True)
        return {"path": str(path), "base": base, "base_sha": head}

    monkeypatch.setattr(dispatcher, "_create_worktree", prepare)
    return repo, head


def _source(home: Path, repo: Path, run_id: str) -> None:
    manifest = home / "runs" / run_id / "manifest.md"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        f"node: {run_id}\nstatus: in-progress\ncommits: {run_id}\n",
        encoding="utf-8",
    )
    runs._write_json(
        runs.pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "session": SESSION,
            "session_id": "synthetic-worker-session",
            "repo": str(repo),
            "worktree": str(repo),
            "role": "implement",
            "node": {"id": run_id, "plan": "fixture", "section": "s2"},
            "backend": "alpha",
            "launch": "cli",
            "argv": ["codex"],
            "phase": "starting",
            "process_alive": False,
            "launcher_host": socket.gethostname(),
            "pid": 99999999,
            "manifest_path": str(manifest),
        },
    )


def _deliver(home: Path, run_id: str) -> None:
    manifest = home / "runs" / run_id / "manifest.md"
    manifest.write_text(
        f"node: {run_id}\nstatus: complete\ncommits: {run_id}\n",
        encoding="utf-8",
    )


def test_changed_session_runs_its_review_and_repair_on_the_next_requested_sweep(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, head = _crew(home, tmp_path, monkeypatch)
    review_id = "r-needs-review"
    repair_id = "r-needs-repair"
    _source(home, repo, review_id)
    _source(home, repo, repair_id)
    entered = threading.Event()
    release = threading.Event()
    calls: list[tuple[str, str]] = []
    launches: list[object] = []
    reports: list[dict] = []
    timings: list[dict[str, str]] = []

    def launch(plan: object, **_kwargs: object) -> int:
        launches.append(plan)
        return os.getpid()

    def slow_plan_pass(*_args: object, **_kwargs: object) -> dict:
        if len(calls) == 1:
            entered.set()
            assert release.wait(timeout=20), "the controlled sweep was not released"
        return {}

    monkeypatch.setattr(
        resumption.plan_review, "compose_owed_plan_reviews", slow_plan_pass
    )
    monkeypatch.setattr(resumption, "external_wait", lambda *a, **k: None)
    monkeypatch.setattr(resumption, "_stream_refusal_block", lambda *a, **k: None)

    def sweep(project: str, *, session: str, **kwargs: object) -> dict:
        calls.append((project, session))
        timing = {"started": datetime.now(UTC).isoformat()}
        timings.append(timing)
        result = resumption.sweep(
            project, session=session, config=CONFIG, launcher=launch, **kwargs
        )
        timing["finished"] = datetime.now(UTC).isoformat()
        reports.append(result)
        return result

    threads: list[threading.Thread] = []

    def start(run) -> None:
        thread = threading.Thread(target=run)
        threads.append(thread)
        thread.start()

    store = station_store.StationStore(tmp_path / "station")
    beat = station.SweepBeat(sweep=sweep, start=start)
    node = station.Station(store, home=home / "crew", projects=dict, sweeps=beat)
    with runs.follower_claim(PROJECT, SESSION, delivery="stream"):
        first_requested = datetime.now(UTC).isoformat()
        node.tick(now=MOMENT)
        assert entered.wait(timeout=20), "the station never requested the first sweep"
        try:
            _deliver(home, review_id)
            _deliver(home, repair_id)
            review.store_review(
                {
                    "project": PROJECT,
                    "reviewed_run_id": repair_id,
                    "reviewed_base_sha": head,
                    "reviewed_head_sha": head,
                    "status": "parsed",
                    "scores": dict.fromkeys(review.REVIEW_DIMENSIONS, 10),
                    "total": 50,
                    "findings": [
                        {
                            "file": "module.py",
                            "line": "1",
                            "text": "value remains incorrect",
                            "severity": "blocking",
                        }
                    ],
                }
            )
            second_requested = datetime.now(UTC).isoformat()
            node.tick(now=MOMENT + timedelta(seconds=station.SWEEP_BEAT_SECONDS + 1))
            assert len(calls) == 1
        finally:
            release.set()
            for thread in threads:
                thread.join(timeout=20)
    assert all(not thread.is_alive() for thread in threads)
    assert calls == [(PROJECT, SESSION), (PROJECT, SESSION)]
    review_dispatch = runs.read_pointer(review_id)["review_dispatch"]
    repair_dispatch = runs.read_pointer(repair_id)["repair_dispatch"]
    assert reports[0]["reviews"]["dispatched"] == []
    assert review_dispatch["status"] == "dispatched"
    assert reports[1]["reviews"]["dispatched"] == [review_dispatch["run_id"]]
    review_run = runs.read_pointer(review_dispatch["run_id"])
    assert review_run is not None and review_run["role"] == "review"
    assert repair_dispatch["status"] == "resumed"
    assert repair_dispatch["round_id"]
    advice = home / "crew" / "runs" / repair_id / "resume-1-advice.txt"
    assert "value remains incorrect" in advice.read_text(encoding="utf-8")
    assert len(launches) == 2
    assert first_requested <= timings[0]["started"] < second_requested
    assert second_requested < timings[0]["finished"] <= timings[1]["started"]
    assert timings[1]["started"] <= timings[1]["finished"]
    (tmp_path / "sweep-timing.json").write_text(
        json.dumps(
            {"requests": [first_requested, second_requested], "sweeps": timings},
            indent=2,
        ),
        encoding="utf-8",
    )
