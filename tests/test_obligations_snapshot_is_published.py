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
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from reckon.crew import obligation_snapshot, recovery, runs

obligations_module = importlib.import_module("reckon.crew.obligations")

PROJECT = "snapshot-fixture"
SESSION = "coordinator-fixture"
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT_MODULE = REPOSITORY_ROOT / "reckon" / "crew" / "obligation_snapshot.py"
DERIVATION_MODULES = ("reckon._plan_html", "reckon._backends", "reckon.ledger")
FRESHNESS = obligation_snapshot.FRESHNESS_WINDOW_SECONDS
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


def _write_pointer(fleet: dict[str, Path], run_id: str, *, phase: str, status: str) -> None:
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


def _sweep() -> None:
    """Drive exactly one producer sweep."""
    runs.list_live(project=PROJECT)


def test_a_pointer_transition_is_in_the_snapshot_by_the_next_sweep(
    fleet: dict[str, Any],
) -> None:
    """A transition is visible to a reader by the end of the first sweep after it."""
    _write_pointer(fleet, "r-before", phase="working", status="working")
    with runs.follower_registration(PROJECT, SESSION):
        with runs._project_watch_claim(PROJECT, "1h") as (acquired, _seat):
            assert acquired is True
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
    monkeypatch.setattr(
        recovery, "_utc_seconds", lambda: fleet["frozen"].timestamp()
    )
    _write_pointer(fleet, "r-owed", phase="complete", status="complete")
    _write_pointer(fleet, "r-working", phase="working", status="working")
    with runs.follower_registration(PROJECT, SESSION):
        with runs._project_watch_claim(PROJECT, "1h") as (acquired, _seat):
            assert acquired is True
            stored = obligation_snapshot.read_snapshot(PROJECT, SESSION)

    assert stored is not None
    expected = obligations_module.obligations(PROJECT, SESSION)
    assert expected["obligations"], "the parity case must compare a non-empty list"
    # The age fields are the one deliberate rebasing: the snapshot stores the
    # instant each age is measured from, so reading it at the instant it was
    # computed must reproduce the derivation exactly, ages included.
    assert obligation_snapshot.live_payload(
        stored, now=_computed_at(stored)
    ) == expected  # __T5__


def _stopped_between_write_and_rename(staging: str, destination: str) -> None:
    """Stand in for a writer killed after its temporary write."""
    raise OSError(f"stopped before renaming {staging} over {destination}")


def test_a_writer_stopped_before_the_rename_leaves_the_previous_snapshot(
    fleet: dict[str, Any],
) -> None:
    """A snapshot writer that dies mid-write leaves the last snapshot readable."""
    _write_pointer(fleet, "r-owed", phase="complete", status="complete")
    with runs.follower_registration(PROJECT, SESSION):
        with runs._project_watch_claim(PROJECT, "1h") as (acquired, _seat):
            assert acquired is True
            previous = obligation_snapshot.read_snapshot(PROJECT, SESSION)
    assert previous is not None
    path = obligation_snapshot.snapshot_path(PROJECT, SESSION)
    temps_before = [p for p in path.parent.iterdir() if p != path]

    with pytest.raises(OSError):
        obligation_snapshot.write_snapshot(
            PROJECT,
            SESSION,
            {"project": PROJECT, "session": SESSION, "obligations": [], "summary": {}},
            replace=_stopped_between_write_and_rename,
        )

    assert json.loads(path.read_text(encoding="utf-8")) == previous
    assert obligation_snapshot.read_snapshot(PROJECT, SESSION) == previous
    temps_after = [p for p in path.parent.iterdir() if p != path]
    assert len(temps_after) > len(temps_before), "the stopped write left its temp"


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
    assert obligation_snapshot.freshness(dead, current_stamp="stamp-one") == "no-producer"
    assert (
        obligation_snapshot.freshness(stale_code, current_stamp="stamp-one")
        == "producer-stale-code"
    )
    assert (
        obligation_snapshot.freshness(stale_age, current_stamp="stamp-one")
        == "stale-snapshot"
    )
    assert obligation_snapshot.freshness(None, current_stamp="stamp-one") == "no-producer"


def test_a_file_change_and_the_floor_tick_each_republish(fleet: dict[str, Any]) -> None:
    """The other two triggers, and the guard that nothing else republishes."""
    _write_pointer(fleet, "r-working", phase="working", status="working")
    with runs.follower_registration(PROJECT, SESSION):
        with runs._project_watch_claim(PROJECT, "1h") as (acquired, _seat):
            assert acquired is True
            first = obligation_snapshot.read_snapshot(PROJECT, SESSION)
            assert first is not None

            # No trigger: the sweep leaves the snapshot where it is.
            _sweep()
            assert obligation_snapshot.read_snapshot(PROJECT, SESSION) == first

            # A plan file's stat identity moved, with no pointer change at all.
            plan = fleet["repo"] / "docs" / "plans" / "fixture-plan.html"
            plan.write_text("<html>edited</html>\n", encoding="utf-8")
            _sweep()
            after_edit = obligation_snapshot.read_snapshot(PROJECT, SESSION)
            assert after_edit is not None and after_edit != first

            # The floor tick republishes with no event of any kind.
            written = obligation_snapshot.sweep(
                PROJECT,
                sessions=[SESSION],
                producer=after_edit["producer"],
                stream_offset=first["stream_offset"],
                state_dirs=[
                    fleet["repo"] / "docs" / "state" / PROJECT,
                    fleet["repo"] / "docs" / "plans",
                ],
                now=_computed_at(after_edit) + timedelta(seconds=FLOOR + 1),
            )

    assert written, "the floor tick republishes with no other event"