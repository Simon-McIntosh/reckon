"""An explicit lane launches while its advisory picker is still working."""

from __future__ import annotations

import importlib
import json
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from reckon import ledger
from reckon.crew.node import TaskNode
from reckon.crew.runs import read_pointer

dispatch = importlib.import_module("reckon.crew.dispatch")


@pytest.fixture
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    root = tmp_path / "repo"
    plans = root / "docs" / "plans"
    scripts = root / "skills" / "reckon-build" / "scripts"
    plans.mkdir(parents=True)
    scripts.mkdir(parents=True)
    source = Path(__file__).parents[1] / "skills/reckon-build/scripts/worktree_fleet.py"
    (scripts / source.name).write_text(source.read_text())
    (plans / "example.html").write_text(
        '<!doctype html><html><head><meta name="docs-project" content="proj">'
        '<meta name="reckon-type" content="plan">'
        '<meta name="plan-slug" content="example"></head>'
        '<body><h2 id="dispatch">Dispatch</h2></body></html>'
    )
    (root / "seed.txt").write_text("seed\n")
    for args in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "seed.txt", "skills", "docs/plans/example.html"),
        ("commit", "-q", "-m", "chore: seed repository"),
    ):
        subprocess.run(("git", *args), cwd=root, check=True, capture_output=True)
    (home / "mounts.json").write_text(json.dumps({"proj": str(root / "docs")}))
    return root


def test_explicit_lane_returns_before_slow_shadow_and_records_it(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from reckon.crew import picker

    entered = threading.Event()
    release = threading.Event()
    picker_done = threading.Event()
    workers: list[threading.Thread] = []

    def slow_pick(*_args, **_kwargs):
        entered.set()
        assert release.wait(4), "the slow picker was never released"
        picker_done.set()
        return SimpleNamespace(
            as_dict=lambda: {
                "action": "route",
                "backend": "local",
                "family": "test",
                "model": "test-model",
                "effort": "medium",
                "probabilities": {"local": 0.9},
                "confidence": 0.9,
                "jev_model": "test",
                "fallback_reason": None,
                "latency_ms": 2000.0,
                "excluded": [],
            }
        )

    original_popen = subprocess.Popen

    def detached_runner(argv, **kwargs):
        if "_record_shadow_picker_selection" not in str(argv):
            return original_popen(argv, **kwargs)
        worker = threading.Thread(
            target=dispatch._record_shadow_picker_selection,
            args=(Path(argv[-1]),),
            daemon=True,
        )
        workers.append(worker)
        worker.start()
        return SimpleNamespace(pid=12345)

    monkeypatch.setattr(picker, "pick", slow_pick)
    monkeypatch.setattr(dispatch.subprocess, "Popen", detached_runner)
    node = TaskNode(
        id="shadow-probe",
        goal="record a dispatch while the picker works",
        plan="example",
        section="dispatch",
        spec_level="guided",
        done_when="the test records the selection after dispatch returns",
        write_paths=["output.txt"],
        time_budget="20m",
        manifest_path=str(tmp_path / "worker.md"),
    )
    config = {
        "default_backend": "local",
        "local_backend": "local",
        "backends": {
            "local": {
                "launch": "in-harness",
                "model": "test-model",
                "sandbox": "worktree-full",
                "time_budget": "20m",
            }
        },
        "roles": {"implement": {}},
        "fences": {"time_budget": "20m", "needs_help_after_failures": 2},
    }
    started = time.monotonic()
    try:
        record = dispatch.dispatch(
            node=node,
            project="proj",
            repo=repository,
            config=config,
            session="test-session",
            local=True,
            route="deterministic",
            check_budget=False,
            watch_required=False,
        )
        elapsed = time.monotonic() - started
        assert not picker_done.is_set(), (
            f"the slow picker finished before dispatch returned in {elapsed:.3f}s"
        )
        assert record["route_mode"] == "explicit"
        assert record["picker_selection"] is None
        assert entered.wait(2), "the shadow picker never ran"
    finally:
        release.set()
    for worker in workers:
        worker.join(timeout=5)
        assert not worker.is_alive()
    pointer = read_pointer(record["run_id"])
    assert pointer["picker_selection"]["backend"] == "local"
    row = ledger.build_record(run_id=record["run_id"], plan="example", gate="not-run")
    assert row["picker_selection"]["backend"] == "local"


def test_detached_picker_process_publishes_to_pointer(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RECKON_PICKER_CREDENTIAL", str(tmp_path / "missing-key"))
    config = {
        "default_backend": "local",
        "local_backend": "local",
        "backends": {
            "local": {
                "launch": "in-harness",
                "model": "test-model",
                "sandbox": "worktree-full",
                "time_budget": "20m",
            }
        },
        "roles": {"implement": {}},
        "fences": {"time_budget": "20m", "needs_help_after_failures": 2},
    }
    record = dispatch.dispatch(
        node=TaskNode(
            id="detached-probe",
            goal="record a detached picker answer",
            plan="example",
            section="dispatch",
            spec_level="guided",
            done_when="the test observes the run pointer selection",
            write_paths=["detached-output.txt"],
            time_budget="20m",
            manifest_path=str(tmp_path / "detached-worker.md"),
        ),
        project="proj",
        repo=repository,
        config=config,
        session="test-session",
        local=True,
        route="deterministic",
        check_budget=False,
        watch_required=False,
    )
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        pointer = read_pointer(record["run_id"])
        if pointer.get("picker_selection") is not None:
            break
        time.sleep(0.05)
    assert pointer["picker_selection"]["action"] in {
        "route",
        "refuse",
        "fallback",
        "hold",
    }
    assert "latency_ms" in pointer["picker_selection"]
    row = ledger.build_record(run_id=record["run_id"], plan="example", gate="not-run")
    assert row["picker_selection"] == pointer["picker_selection"]
