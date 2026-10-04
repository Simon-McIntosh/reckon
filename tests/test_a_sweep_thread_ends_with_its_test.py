"""A sweep thread does not outlive the test that started it.

The producer starts an obligations sweep on a daemon thread off its own
transition path and never joins it, so a sweep one test triggers can still be
running while the next test has replaced the modules it reads. These cases pin
the join that ends it: a transition starts a sweep whose body holds in flight,
the next test replaces the ledger decoder with a refusal and asserts that no
thread exception reaches it, and the helper is called directly against a held
sweep. The refusal test writes the seat record a retained tree would leave
behind into its own tree, which the per-test reap reads at that test's
teardown; every forgery is scoped to the test body, so the reap decodes the
record with the real readers rather than with a refusal. The fleet and the
configuration home are synthesised under the test's own temporary paths, so
no case reaches a real project or a real producer.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from reckon.crew import ledger, runs

PROJECT = "sweep-fixture"
SESSION = "coordinator-fixture"

# How long a held sweep stays in flight once its trigger starts it: long
# enough that a teardown which does not join the thread leaves it running into
# the next test, and short enough that a teardown which does join it finishes
# well inside the join's bound.
HOLD_SECONDS = 2.0

SWEEP_THREAD_PREFIX = "reckon-obligations-sweep"


def _git(root: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


@pytest.fixture()
def fleet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """A synthesised project under a temporary configuration home."""
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    (root / "docs" / "plans").mkdir()
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "seed.txt"),
        ("commit", "-q", "-m", "test: seed the sweep fixture"),
    ):
        _git(root, *arguments)
    (home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return {"home": home, "repo": root}


def _write_pointer(fleet: dict[str, Any], run_id: str, *, phase: str) -> None:
    """Write one live pointer and the manifest its delivery is read from."""
    root = fleet["repo"]
    manifest = fleet["home"] / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    head = _git(root, "rev-parse", "HEAD")
    manifest.write_text(
        f"node: {run_id}\nstatus: {phase}\ncommits: [{head}]\n",
        encoding="utf-8",
    )
    runs._write_json(
        runs.pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "session": SESSION,
            "repo": str(root),
            "worktree": str(root),
            "base_sha": head,
            "process_alive": False,
            "phase": phase,
            "launch": "in-harness",
            "role": "implement",
            "manifest_path": str(manifest),
            "node": {
                "id": run_id,
                "plan": "fixture-plan",
                "section": "fixture-section",
                "time_budget": "20m",
                "write_paths": ["seed.txt"],
            },
        },
    )


def _sweep_threads() -> list[threading.Thread]:
    """The obligation sweep threads alive in this process, by their name."""
    return [
        thread
        for thread in threading.enumerate()
        if thread.name.startswith(SWEEP_THREAD_PREFIX)
    ]


def _await_no_sweep_threads(timeout: float = 10.0) -> None:
    """Wait, bounded, for every sweep thread in this process to end."""
    deadline = time.monotonic() + timeout
    while True:
        alive = _sweep_threads()
        if not alive:
            return
        if time.monotonic() >= deadline:
            raise AssertionError(
                "sweep threads still running: "
                + ", ".join(sorted(thread.name for thread in alive))
            )
        time.sleep(0.05)


def _start_held_sweep(
    monkeypatch: pytest.MonkeyPatch, fleet: dict[str, Any], started: threading.Event
) -> str:
    """Register a producer and trigger one sweep that holds in its body.

    The hold stands in for a sweep that is slow to finish — a plan read, a
    ledger decode — and ends on its own, so a caller that joins the thread
    sees it finish while a caller that does not leaves it running. Returns the
    thread's name, so a failure names the sweep that was left behind.

    The patch lives inside a context that ends with this call, and the sweep
    is inside the held body before it ends, so no teardown fixture runs under
    a forged process-global reader.
    """
    real_publish = runs._publish_obligation_snapshots

    def held(project: str, *, transition_fired: bool) -> list[str]:
        started.set()
        time.sleep(HOLD_SECONDS)
        return real_publish(project, transition_fired=transition_fired)

    with monkeypatch.context() as scoped:
        scoped.setattr(runs, "_publish_obligation_snapshots", held)
        runs._WATCH_STREAM_PRODUCERS[PROJECT] = runs._WatchStreamProducer(
            path=runs.watch_stream_path(PROJECT),
            known={},
            stall_window=runs.DEFAULT_WATCH_STALL_WINDOW,
        )
        _write_pointer(fleet, "r-held", phase="working")
        runs.list_live(project=PROJECT)
        assert started.wait(timeout=10.0), "a transition must start a sweep"
    return f"{SWEEP_THREAD_PREFIX}-{PROJECT}"


def test_a_transition_starts_a_sweep_held_past_this_test(
    fleet: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A watch transition starts a sweep, and this test ends while it runs."""
    started = threading.Event()
    thread_name = _start_held_sweep(monkeypatch, fleet, started)

    alive = [thread.name for thread in _sweep_threads()]
    assert thread_name in alive, (
        "this test must end with the sweep it started still in flight; "
        f"live sweep threads: {alive}"
    )


def test_no_sweep_thread_exception_reaches_the_next_test(
    fleet: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sweep left running would decode here; the join means it cannot.

    The previous test's sweep was either ended by teardown or is still in
    flight. This test replaces the ledger decoder with a refusal and waits for
    any sweep thread to end: a sweep that outlived its test reaches the
    refusal and dies with the exception, which is recorded here and fails this
    test. With the join in place there is no thread to reach it.

    A failed test's tree is retained with the seat record under it, and the
    per-test reap decodes every record under the basetemp during a later
    test's teardown — under whatever process-global readers that test left
    installed. This test writes that record into its own tree, which lives
    through this teardown because the reap requests ``tmp_path`` and so is
    finalized before the tree is pruned; a sibling test's tree is pruned
    before this test's teardown and would never be seen by it. Both forgeries
    live inside a context that ends with the wait, so the reap here reads a
    retained record with the real decoder — a refusal left installed past the
    wait turns it into a teardown error instead.
    """
    record = tmp_path / "crew" / "watch" / "seat.lock"
    record.parent.mkdir(parents=True)
    record.write_text("{}\n", encoding="utf-8")

    seen: list[BaseException] = []
    previous = threading.excepthook

    def observed(args: threading.ExceptHookArgs) -> None:
        seen.append(args.exc_value)
        previous(args)

    def refuse(*args: object, **kwargs: object) -> object:
        raise AssertionError("a sweep outlived its test and decoded the ledger")

    with monkeypatch.context() as scoped:
        scoped.setattr(threading, "excepthook", observed)
        scoped.setattr(ledger.json, "loads", refuse)
        _await_no_sweep_threads()
    assert seen == [], f"an earlier test's sweep thread raised here: {seen!r}"


def test_the_helper_joins_the_sweep_and_clears_the_registry(
    fleet: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The helper returns only after the held sweep has ended."""
    started = threading.Event()
    _start_held_sweep(monkeypatch, fleet, started)
    assert runs._WATCH_STREAM_PRODUCERS, "a registered producer is what the join sees"

    stragglers = runs.join_watch_sweeps()

    assert stragglers == [], "the held sweep must end inside the join's bound"
    assert runs._WATCH_STREAM_PRODUCERS == {}, "the registry must be cleared"
    assert _sweep_threads() == [], "no sweep thread may be alive after the join"
