"""The producer publishes each registered session's obligations snapshot.

The snapshot is what a coordinator's prompt hook reads instead of deriving, so
these cases pin the three things that makes true: a transition reaches the
snapshot by the end of the first sweep after it, the snapshot carries the
derivation unchanged over unmodified files, and a writer stopped between its
temporary file and the rename leaves the previous snapshot whole. They also pin
the freshness vocabulary and the module's import surface, because the hook
loads this module and must not reach the derivation through it.

The fleet is synthesised under the test's own temporary path and a temporary
configuration home: sweeps are driven explicitly, never by waiting on a clock.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import os
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from reckon.crew import recovery_classification
from reckon.crew import recovery_liveness
from reckon.crew import recovery_stream
from reckon.crew import recovery_watch
from reckon.crew import obligation_snapshot, recovery, runs
from reckon.crew import review as review_module

obligations_module = importlib.import_module("reckon.crew.obligations")

PROJECT = "snapshot-fixture"
SESSION = "coordinator-fixture"
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT_MODULE = REPOSITORY_ROOT / "reckon" / "crew" / "obligation_snapshot.py"
DERIVATION_MODULES = ("reckon._plan_html", "reckon._backends", "reckon.ledger")
FRESHNESS = obligation_snapshot.FRESHNESS_WINDOW_SECONDS
STAT = obligation_snapshot.STAT_IDENTITY_INTERVAL_SECONDS
FLOOR = obligation_snapshot.FLOOR_TICK_SECONDS

# The module is imported by path: `import reckon.crew.obligation_snapshot`
# runs it through `reckon/crew.py`, which eagerly imports every concern
# module, so a package import would report the derivations whether or not this
# module reaches them. Importing the file alone observes what this module
# itself pulls in, which is what the hook's prompt path is exposed to.
PROBE = (
    "import runpy, sys; g = runpy.run_path(sys.argv[1], run_name='__main__');"
    " print('loaded', g['__file__'], g['snapshot_path'].__name__);"
    " print([m for m in sys.argv[2:] if m in sys.modules])"
)


def test_importing_the_snapshot_module_pulls_in_no_derivation() -> None:
    """The module's own import surface reaches no derivation module."""
    completed = subprocess.run(
        [sys.executable, "-c", PROBE, str(SNAPSHOT_MODULE), *DERIVATION_MODULES],
        capture_output=True,
        text=True,
        cwd=str(REPOSITORY_ROOT),
        env={**os.environ, "PYTHONPATH": str(REPOSITORY_ROOT)},
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    lines = completed.stdout.splitlines()
    assert lines and lines[0].startswith("loaded"), completed.stdout
    assert lines[1] == "[]", completed.stdout


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _commit(repository: Path, name: str, body: str) -> str:
    """Add one file at the repository root and return the new revision."""
    (repository / name).write_text(body, encoding="utf-8")
    _git(repository, "add", name)
    _git(repository, "commit", "-q", "-m", f"test: add {name}")
    return _git(repository, "rev-parse", "HEAD")


@pytest.fixture()
def fleet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """A synthesised project under a temporary configuration home."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    (root / "docs" / "plans").mkdir()
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "seed.txt"),
        ("commit", "-q", "-m", "test: seed the snapshot fixture"),
    ):
        _git(root, *arguments)
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    frozen = datetime.now(tz=UTC)
    monkeypatch.setattr(obligations_module, "_utc_now", lambda: frozen)
    return {"home": config_home, "repo": root, "frozen": frozen}


def _write_pointer(
    fleet: dict[str, Path], run_id: str, *, phase: str, status: str
) -> None:
    """Write one live pointer and the manifest its delivery is read from."""
    root = fleet["repo"]
    manifest = fleet["home"] / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    head = _git(root, "rev-parse", "HEAD")
    manifest.write_text(
        f"node: {run_id}\nstatus: {status}\ncommits: [{head}]\n",
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


def _wait_for_sweep() -> None:
    """Wait for the producer's sweep thread to finish publishing."""
    for _ in range(600):
        producer = runs._WATCH_STREAM_PRODUCERS.get(PROJECT)
        thread = None if producer is None else producer.sweep_thread
        if thread is None:
            return
        thread.join(timeout=0.1)
    raise AssertionError("the sweep did not settle")


def _sweep() -> None:
    """Drive exactly one producer sweep and wait for it to publish."""
    runs.list_live(project=PROJECT)
    _wait_for_sweep()


def test_a_pointer_transition_is_in_the_snapshot_by_the_next_sweep(
    fleet: dict[str, Any],
) -> None:
    """A transition is visible to a reader by the end of the first sweep after it."""
    _write_pointer(fleet, "r-before", phase="working", status="working")
    with (
        runs.follower_registration(PROJECT, SESSION, delivery="stream"),
        runs._project_watch_claim(PROJECT, "1h") as (acquired, _seat),
    ):
        assert acquired is True
        _wait_for_sweep()
        first = obligation_snapshot.read_snapshot(PROJECT, SESSION)
        assert first is not None, "the producer's first sweep publishes a baseline"
        kinds_before = [item["kind"] for item in first["obligations"]]

        _write_pointer(fleet, "r-before", phase="complete", status="complete")
        _sweep()
        second = obligation_snapshot.read_snapshot(PROJECT, SESSION)

    assert second is not None
    kinds_after = [item["kind"] for item in second["obligations"]]
    assert kinds_after, "the delivered run owes a review"
    assert kinds_after != kinds_before
    assert second["stream_offset"] > first["stream_offset"]


def _computed_at(document: dict) -> datetime:
    text = str(document["computed_at"]).replace("Z", "+00:00")
    return datetime.fromisoformat(text)


def test_the_snapshot_carries_the_derivation_over_unmodified_files(
    fleet: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stored snapshot equals the derivation over the same unmodified files."""
    # Both derivations are read at one instant: without this the classifier's
    # own clock moves between the sweep and the comparison and an age that
    # ticked forward would read as a content diff. It is the *inputs* that must
    # be unmodified, not the wall clock.
    (monkeypatch.setattr(recovery_stream, "_utc_seconds", fleet["frozen"].timestamp), monkeypatch.setattr(recovery_liveness, "_utc_seconds", fleet["frozen"].timestamp), monkeypatch.setattr(recovery_classification, "_utc_seconds", fleet["frozen"].timestamp), monkeypatch.setattr(recovery_watch, "_utc_seconds", fleet["frozen"].timestamp))
    _write_pointer(fleet, "r-owed", phase="complete", status="complete")
    _write_pointer(fleet, "r-working", phase="working", status="working")
    with (
        runs.follower_registration(PROJECT, SESSION, delivery="stream"),
        runs._project_watch_claim(PROJECT, "1h") as (acquired, _seat),
    ):
        assert acquired is True
        _wait_for_sweep()
        stored = obligation_snapshot.read_snapshot(PROJECT, SESSION)

    assert stored is not None
    expected = obligations_module.obligations(PROJECT, SESSION)
    assert expected["obligations"], "the parity case must compare a non-empty list"
    # The age fields are the one deliberate rebasing: the snapshot stores the
    # instant each age is measured from, so reading it at the instant it was
    # computed must reproduce the derivation exactly, ages included.
    assert (
        obligation_snapshot.live_payload(stored, now=_computed_at(stored)) == expected
    )


def _stopped_between_write_and_rename(staging: str, destination: str) -> None:
    """Stand in for a writer killed after its temporary write."""
    raise OSError(f"stopped before renaming {staging} over {destination}")


def test_a_writer_stopped_before_the_rename_leaves_the_previous_snapshot(
    fleet: dict[str, Any],
    monkeypatch,
) -> None:
    """An interrupted publication preserves the snapshot and removes its temporary."""
    _write_pointer(fleet, "r-owed", phase="complete", status="complete")
    with (
        runs.follower_registration(PROJECT, SESSION, delivery="stream"),
        runs._project_watch_claim(PROJECT, "1h") as (acquired, _seat),
    ):
        assert acquired is True
        _wait_for_sweep()
        previous = obligation_snapshot.read_snapshot(PROJECT, SESSION)
    assert previous is not None
    path = obligation_snapshot.snapshot_path(PROJECT, SESSION)
    temps_before = [p for p in path.parent.iterdir() if p != path]

    monkeypatch.setattr(os, "replace", _stopped_between_write_and_rename)
    with pytest.raises(OSError, match="stopped before renaming"):
        obligation_snapshot.write_snapshot(
            PROJECT,
            SESSION,
            {"project": PROJECT, "session": SESSION, "obligations": [], "summary": {}},
        )

    assert json.loads(path.read_text(encoding="utf-8")) == previous
    assert obligation_snapshot.read_snapshot(PROJECT, SESSION) == previous
    temps_after = [p for p in path.parent.iterdir() if p != path]
    assert temps_after == temps_before, "the interrupted write removed its temporary"


def _document(
    *, pid: object, start: object, stamp: str, computed_at: datetime
) -> dict[str, Any]:
    """A stored snapshot body carrying one chosen producer identity."""
    return obligation_snapshot.document_for(
        {
            "project": PROJECT,
            "session": SESSION,
            "obligations": [],
            "acknowledged": [],
            "summary": {"count": 0, "oldest_age_seconds": 0, "unreconciled_runs": 0},
        },
        computed_at=computed_at,
        stream_offset=0,
        producer={
            "pid": pid,
            "pid_start_time": start,
            "started_at": None,
            "code_stamp": stamp,
        },
    )


def test_each_freshness_outcome_is_returned_for_its_case() -> None:
    """Every reason a snapshot is not fresh, and the fresh case, have one word."""
    now = datetime.now(tz=UTC)
    live = os.getpid()
    start = obligation_snapshot.process_start_time(live)
    assert start is not None
    finished = subprocess.Popen(["true"])
    finished.wait()
    dead_pid = finished.pid

    fresh = _document(pid=live, start=start, stamp="stamp-one", computed_at=now)
    assert obligation_snapshot.freshness(fresh, current_stamp="stamp-one") == "fresh"
    reused_pid = _document(pid=live, start="0", stamp="stamp-one", computed_at=now)
    dead = _document(pid=dead_pid, start=start, stamp="stamp-one", computed_at=now)
    stale_code = _document(pid=live, start=start, stamp="old", computed_at=now)
    stale_age = _document(
        pid=live,
        start=start,
        stamp="stamp-one",
        computed_at=now - timedelta(seconds=FRESHNESS + 1),
    )

    assert (
        obligation_snapshot.freshness(reused_pid, current_stamp="stamp-one")
        == "no-producer"
    )
    assert (
        obligation_snapshot.freshness(dead, current_stamp="stamp-one") == "no-producer"
    )
    assert (
        obligation_snapshot.freshness(stale_code, current_stamp="stamp-one")
        == "producer-stale-code"
    )
    assert (
        obligation_snapshot.freshness(stale_age, current_stamp="stamp-one")
        == "stale-snapshot"
    )
    assert (
        obligation_snapshot.freshness(None, current_stamp="stamp-one") == "no-producer"
    )


def _sweep_memory_row(module: Any, key: str) -> str:
    """The _SWEEPS entry one module instance holds for a key, or its absence."""
    memory = getattr(module, "_SWEEPS", {}).get(key)
    if memory is None:
        return "absent"
    identity = getattr(memory, "identity", None)
    return (
        f"checked_at={getattr(memory, 'checked_at', None)}"
        f" published_at={getattr(memory, 'published_at', None)}"
        f" identity_files={len(identity) if isinstance(identity, dict) else identity!r}"
    )


def _divergence_report(*, consulted: str | None = None) -> str:
    """Name the state a republish assertion failed against.

    Each reading names a way the memory a sweep consults can diverge from the
    producer's write: the cache key (a moved config home moves it), the entry
    for it -- absent or stale -- as the caller read it just before a sweep
    (``consulted``) or at failure, whether sys.modules holds the instance the
    test patched or a second one, the resolved config home, and the live
    thread names.
    """
    key = str(obligation_snapshot.snapshot_dir(PROJECT))
    registered = sys.modules.get("reckon.crew.obligation_snapshot")
    entry = (
        f"before the call: {consulted}"
        if consulted is not None
        else f"at failure: {_sweep_memory_row(obligation_snapshot, key)}"
    )
    lines = [f"snapshot key: {key}", f"_SWEEPS entry for the key {entry}"]
    if registered is obligation_snapshot:
        lines.append("sys.modules holds the test's imported instance")
    else:
        lines.append(
            "sys.modules holds a different instance:"
            f" test={getattr(obligation_snapshot, '__file__', '?')}"
            f" sys.modules={getattr(registered, '__file__', '?')}"
        )
        if registered is not None:
            lines.append(f"  its _SWEEPS entry: {_sweep_memory_row(registered, key)}")
    lines.append(
        f"RECKON_HOME: {os.environ.get('RECKON_HOME')!r} resolved to"
        f" {obligation_snapshot._config_home()}"
    )
    lines.append(
        "live threads: " + ", ".join(sorted(t.name for t in threading.enumerate()))
    )
    return "\n".join(lines)


def test_a_file_change_and_the_floor_tick_each_republish(fleet: dict[str, Any]) -> None:
    """The other two triggers, and the guard that nothing else republishes."""
    _write_pointer(fleet, "r-working", phase="working", status="working")
    with (
        runs.follower_registration(PROJECT, SESSION, delivery="stream"),
        runs._project_watch_claim(PROJECT, "1h") as (acquired, _seat),
    ):
        assert acquired is True
        _wait_for_sweep()
        first = obligation_snapshot.read_snapshot(PROJECT, SESSION)
        assert first is not None

        # No trigger: the sweep leaves the snapshot where it is.
        _sweep()
        assert obligation_snapshot.read_snapshot(PROJECT, SESSION) == first

        dirs = [
            fleet["repo"] / "docs" / "state" / PROJECT,
            fleet["repo"] / "docs" / "plans",
        ]

    def sweep_at(instant: datetime) -> list[Path]:
        return obligation_snapshot.sweep(
            PROJECT,
            sessions=[SESSION],
            producer=first["producer"],
            stream_offset=first["stream_offset"],
            state_dirs=dirs,
            now=instant,
        )

    # A plan file's stat identity moved, with no pointer change at all — but
    # the walk is on a cadence, so inside it the change is not looked for yet.
    # The entry the sweep will consult is read before the call: a republish
    # here means that entry was absent or stale where the producer's write of
    # ``first`` left one current.
    plan = fleet["repo"] / "docs" / "plans" / "fixture-plan.html"
    plan.write_text("<html>edited</html>\n", encoding="utf-8")
    consulted = _sweep_memory_row(
        obligation_snapshot, str(obligation_snapshot.snapshot_dir(PROJECT))
    )
    republished = sweep_at(_computed_at(first) + timedelta(seconds=5))
    assert republished == [], (
        f"the +5 s sweep, inside the {STAT:g} s stat cadence, republished"
        f" {[str(path) for path in republished]}\n"
        + _divergence_report(consulted=consulted)
    )

    # Past the cadence the same change republishes the session's snapshot.
    assert sweep_at(_computed_at(first) + timedelta(seconds=STAT + 5))
    after_edit = obligation_snapshot.read_snapshot(PROJECT, SESSION)
    assert after_edit is not None and after_edit != first

    # The floor tick republishes with no event of any kind.
    assert sweep_at(_computed_at(after_edit) + timedelta(seconds=FLOOR + 1))


def _released_registration(session: str) -> None:
    """Leave a registration whose process is gone, as a released one looks."""
    finished = subprocess.Popen(["true"])
    finished.wait()
    path = runs.follower_lock_path(PROJECT, session)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"project": PROJECT, "session": session, "pid": finished.pid}),
        encoding="utf-8",
    )


def test_only_a_live_registration_gets_a_snapshot(fleet: dict[str, Any]) -> None:
    """A registration whose follower has gone is not published for."""
    _released_registration("released-fixture")
    _write_pointer(fleet, "r-working", phase="working", status="working")
    with (
        runs.follower_registration(PROJECT, SESSION, delivery="stream"),
        runs._project_watch_claim(PROJECT, "1h") as (acquired, _seat),
    ):
        assert acquired is True
        _wait_for_sweep()
        rows = {row["session"]: row for row in runs.list_followers(PROJECT)}
        assert rows["released-fixture"]["live"] is False, "the fixture must be released"
        assert rows[SESSION]["live"] is True

    assert obligation_snapshot.read_snapshot(PROJECT, SESSION) is not None
    assert obligation_snapshot.read_snapshot(PROJECT, "released-fixture") is None


def test_a_slice_equals_the_derivation_over_the_same_files(
    fleet: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The per-session slice is the derivation, field for field."""
    # Both derivations are read at one instant: the classifier's own clock
    # otherwise moves between them and an age that ticked forward would read as
    # a content diff. It is the files that must be unmodified, not the clock.
    (monkeypatch.setattr(recovery_stream, "_utc_seconds", fleet["frozen"].timestamp), monkeypatch.setattr(recovery_liveness, "_utc_seconds", fleet["frozen"].timestamp), monkeypatch.setattr(recovery_classification, "_utc_seconds", fleet["frozen"].timestamp), monkeypatch.setattr(recovery_watch, "_utc_seconds", fleet["frozen"].timestamp))
    _write_pointer(fleet, "r-owed", phase="complete", status="complete")
    _write_pointer(fleet, "r-working", phase="working", status="working")
    now = fleet["frozen"]
    state = obligation_snapshot.fleet_state(PROJECT, now=now)
    sliced = obligation_snapshot.payload_for(state, SESSION)
    derived = obligations_module.obligations(PROJECT, SESSION)
    assert derived["obligations"], "the slice parity case must compare duties"
    assert sliced == derived


def _store_review(*, run_id: str, base: str, head: str) -> None:
    """Store one complete review record naming the revision it read."""
    emitted = "\n".join(
        f"SCORE {dimension}: 20" for dimension in review_module.REVIEW_DIMENSIONS
    )
    record = review_module.parse_review(emitted)
    record.update(
        {
            "project": PROJECT,
            "reviewed_run_id": run_id,
            "review_run_id": f"review-of-{run_id}",
            "reviewed_base_sha": base,
            "reviewed_head_sha": head,
        }
    )
    review_module.store_review(record)


def test_a_moved_head_names_the_same_two_heads_in_snapshot_and_derivation(
    fleet: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run whose head moved past its stored review reads review-missing.

    The snapshot's duty and the derivation's are compared directly, so a
    snapshot building the review duty by its own path fails: the duty would
    carry no head pair and the two readers would disagree on the run's
    evidence.
    """
    (monkeypatch.setattr(recovery_stream, "_utc_seconds", fleet["frozen"].timestamp), monkeypatch.setattr(recovery_liveness, "_utc_seconds", fleet["frozen"].timestamp), monkeypatch.setattr(recovery_classification, "_utc_seconds", fleet["frozen"].timestamp), monkeypatch.setattr(recovery_watch, "_utc_seconds", fleet["frozen"].timestamp))
    repository = fleet["repo"]
    reviewed_head = _git(repository, "rev-parse", "HEAD")
    head = _commit(repository, "repair.txt", "repair\n")
    assert head != reviewed_head
    _write_pointer(fleet, "r-moved", phase="complete", status="complete")
    _store_review(run_id="r-moved", base=reviewed_head, head=reviewed_head)

    state = obligation_snapshot.fleet_state(PROJECT, now=fleet["frozen"])
    sliced = obligation_snapshot.payload_for(state, SESSION)
    derived = obligations_module.obligations(PROJECT, SESSION)

    assert sliced == derived
    for reader, payload in (("snapshot", sliced), ("derivation", derived)):
        duties = [
            item for item in payload["obligations"] if item["run_id"] == "r-moved"
        ]
        assert len(duties) == 1, (reader, payload["obligations"])
        duty = duties[0]
        assert duty["kind"] == "review-missing", (reader, duty)
        assert duty["head"] == head, (reader, duty)
        assert duty["reviewed_head"] == reviewed_head, (reader, duty)


def test_the_stat_walk_skips_run_records(fleet: dict[str, Any]) -> None:
    """A record under the state directory's runs subtree is not walked."""
    state_dir = fleet["repo"] / "docs" / "state" / PROJECT
    record = state_dir / "runs" / "r-one.json"
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text("{}\n", encoding="utf-8")
    kept = state_dir / "crew.json"
    kept.write_text("{}\n", encoding="utf-8")
    identity = obligation_snapshot._stat_identity(
        [state_dir, fleet["repo"] / "docs" / "plans"]
    )
    assert str(kept) in identity, "a file beside the runs subtree is still walked"
    assert not any("/runs/" in path for path in identity)


def test_a_zombie_producer_is_not_fresh(fleet: dict[str, Any]) -> None:
    """A process that has exited and not been reaped does not hold a snapshot.

    A zombie keeps its process-table entry, so a liveness probe that only reads
    a start time still finds one; the state field is what tells it apart from a
    running producer.
    """
    zombie = subprocess.Popen([sys.executable, "-c", "pass"])
    try:
        deadline = time.monotonic() + 10.0
        start = None
        while time.monotonic() < deadline:
            if obligation_snapshot._process_stat_fields(zombie.pid)[:1] == ["Z"]:
                start = obligation_snapshot.process_start_time(zombie.pid)
                break
            time.sleep(0.05)
        assert start is not None, "the child never became a zombie"
        document = _document(
            pid=zombie.pid,
            start=start,
            stamp="stamp-one",
            computed_at=datetime.now(tz=UTC),
        )
        assert (
            obligation_snapshot.freshness(document, current_stamp="stamp-one")
            == "no-producer"
        )
    finally:
        zombie.wait()


def test_a_slow_sweep_does_not_hold_the_next_transition(
    fleet: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sweep in flight is skipped over, and a transition never waits on it."""
    _write_pointer(fleet, "r-before", phase="working", status="working")
    started = threading.Event()
    release = threading.Event()
    calls = {"n": 0}
    real_sweep = obligation_snapshot.sweep

    def slow_sweep(*args: object, **kwargs: object) -> object:
        calls["n"] += 1
        started.set()
        release.wait(timeout=30.0)
        return real_sweep(*args, **kwargs)

    monkeypatch.setattr(obligation_snapshot, "sweep", slow_sweep)
    try:
        with (
            runs.follower_registration(PROJECT, SESSION, delivery="stream"),
            runs._project_watch_claim(PROJECT, "1h") as (acquired, _seat),
        ):
            assert acquired is True
            assert started.wait(timeout=30.0), (
                "the claim's first sweep did not reach the patched sweep"
                " within 30 s\n" + _divergence_report()
            )
            _write_pointer(fleet, "r-after", phase="working", status="working")
            moment = time.monotonic()
            runs.list_live(project=PROJECT)
            elapsed = time.monotonic() - moment
            assert elapsed < 5.0, "a transition write waited on the sweep"
            assert calls["n"] == 1, "a trigger during a sweep is skipped"
            stream = runs.watch_stream_path(PROJECT).read_text(encoding="utf-8")
            assert "r-after" in stream
    finally:
        release.set()
        _wait_for_sweep()
