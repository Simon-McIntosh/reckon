"""A finished run's directory is retired once nothing can still need it.

Seven run directories are planted in one temporary crew home, one per state a
finished run can be in. A dry run of the retention pass must offer exactly one
to remove — the run that is promoted, reviewed and past its grace window — and
the real run must remove that one and leave the other six. The pass keys on
whether something can still need the directory, not on raw directory age: the
unpromoted fixture is far older than any window and is still kept, which is
what the declared mutation (reduce the predicate to age alone) turns red.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from reckon import flight, ledger
from reckon.crew import gc as gc_module
from reckon.crew import routing

PROJECT = "test"


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


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


def runs_root(home: Path) -> Path:
    root = home / "crew" / "runs"
    root.mkdir(parents=True, exist_ok=True)
    return root


def plant_run(
    repo: Path,
    home: Path,
    run_id: str,
    *,
    now: datetime,
    age: timedelta,
    ledgered: bool = True,
    figures: bool = True,
    review_tier: str | None = None,
    committed_review: bool = False,
) -> Path:
    """Plant one run directory and its ledger row in the chosen state.

    ``age`` sets the directory mtime relative to ``now``; ``ledgered`` decides
    whether a terminal ledger row exists; ``figures`` decides whether the
    stream-derived figures were recorded; ``review_tier`` and
    ``committed_review`` decide whether a review is owed.
    """
    run_dir = runs_root(home) / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "stream.jsonl").write_text("{}\n")
    if ledgered:
        record = ledger.build_record(
            run_id=run_id,
            plan="plan-a",
            gate="passed",
            node="node-a",
            manifest_path=str(run_dir / "stream.jsonl"),
        )
        if figures:
            record["orientation_input_tokens"] = 128_000.0
            record["tool_steps"] = 7.0
        if review_tier is not None:
            record["review_tier"] = review_tier
        ledger.append_run(PROJECT, record, root=repo)
    if committed_review:
        _write_committed_review(repo, run_id)
    stamp = (now - age).timestamp()
    os.utime(run_dir, (stamp, stamp))
    return run_dir


def _write_committed_review(repo: Path, run_id: str) -> None:
    """File a complete review record for one run in the committed store."""
    directory = repo / "docs" / "state" / PROJECT / "reviews" / "run" / run_id
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "r-20260101T000000000000-review.json").write_text(
        json.dumps(
            {
                "review_run_id": "r-20260101T000000000000-review",
                "reviewed_run_id": run_id,
                "reviewed_base_sha": "a" * 40,
                "reviewed_head_sha": "b" * 40,
                "status": "parsed",
                "findings": [],
            }
        )
    )


def write_pointer(home: Path, run_id: str, *, phase: str) -> None:
    live = home / "crew" / "live"
    live.mkdir(parents=True, exist_ok=True)
    (live / f"{run_id}.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "project": PROJECT,
                "worktree": "",
                "phase": phase,
                # A live pid: a dead pid is not a live run, and the pass reaps
                # a ledgered pointer it reads as dead before it judges the
                # directory, so this fixture would not test what it claims to.
                "pid": os.getpid(),
                "launcher_host": os.uname().nodename,
            }
        )
    )


def plant_seven_states(repo: Path, home: Path, now: datetime) -> dict[str, Path]:
    """Plant the seven fixture runs and return their directory paths by name."""
    old = timedelta(days=60)
    return {
        "live-run": plant_run(
            repo, home, "live-run", now=now, age=old, review_tier=None
        ),
        "queued-run": plant_run(
            repo, home, "queued-run", now=now, age=old, review_tier=None
        ),
        # Older than the grace window (so the declared mutation, which reduces
        # the predicate to age alone, lists it and turns the test red) but
        # younger than the unledgered-directory pass's own thirty-day window,
        # which is a different pass and outside this node's scope.
        "unpromoted-run": plant_run(
            repo,
            home,
            "unpromoted-run",
            now=now,
            age=timedelta(days=5),
            ledgered=False,
        ),
        "review-owed-run": plant_run(
            repo, home, "review-owed-run", now=now, age=old, review_tier="full"
        ),
        "figures-missing-run": plant_run(
            repo,
            home,
            "figures-missing-run",
            now=now,
            age=old,
            figures=False,
        ),
        "reviewed-recent-run": plant_run(
            repo,
            home,
            "reviewed-recent-run",
            now=now,
            age=timedelta(hours=1),
            review_tier="full",
            committed_review=True,
        ),
        "reviewed-old-run": plant_run(
            repo,
            home,
            "reviewed-old-run",
            now=now,
            age=old,
            review_tier="full",
            committed_review=True,
        ),
    }


def _pruned_ids(report: dict[str, Any]) -> set[str]:
    return {
        str(row["run_id"])
        for row in report["run_directories"]
        if row["action"] == "prune"
    }


def write_flight_config(home: Path, grace: str) -> None:
    """Write a host flight config declaring one grace window."""
    home.mkdir(parents=True, exist_ok=True)
    (home / "flight.yaml").write_text(f"gc:\n  directory_grace: {grace}\n")


def _reviewed_run(
    repo: Path, home: Path, run_id: str, *, now: datetime, age: timedelta
) -> Path:
    """A promoted run whose review has landed, so only the window can hold it."""
    return plant_run(
        repo,
        home,
        run_id,
        now=now,
        age=age,
        review_tier="full",
        committed_review=True,
    )


def test_the_configured_window_decides_between_two_reviewed_runs(
    tmp_path: Path, monkeypatch
) -> None:
    home = tmp_path / "config"
    monkeypatch.setenv("RECKON_HOME", str(home))
    repo = repository(tmp_path)
    now = datetime(2026, 9, 1, tzinfo=UTC)
    write_flight_config(home, "3h")
    long_ago = _reviewed_run(
        repo, home, "reviewed-four-hours", now=now, age=timedelta(hours=4)
    )
    recent = _reviewed_run(
        repo, home, "reviewed-two-hours", now=now, age=timedelta(hours=2)
    )

    assert gc_module._run_directory_grace_seconds(PROJECT, repo) == 3 * 3600

    report = routing.garbage_collect(repo=repo, project=PROJECT, now=now)

    assert _pruned_ids(report) == {"reviewed-four-hours"}, sorted(_pruned_ids(report))
    assert recent.exists()
    assert long_ago.exists()


def test_a_malformed_window_falls_back_to_the_shipped_default(
    tmp_path: Path, monkeypatch
) -> None:
    home = tmp_path / "config"
    monkeypatch.setenv("RECKON_HOME", str(home))
    repo = repository(tmp_path)
    now = datetime(2026, 9, 1, tzinfo=UTC)
    # A window written into a real config layer is checked by the flight schema
    # where it is written, so a typo is refused there rather than resolving
    # silently. This exercises the other path: a config assembled without that
    # check — an in-process caller, or a layer the validator never saw — reaches
    # the reader carrying the bad value, and the reader falls back to the
    # shipped day instead of failing the sweep or applying no window at all.
    monkeypatch.setattr(
        flight,
        "resolve",
        lambda *args, **kwargs: SimpleNamespace(
            config={"gc": {"directory_grace": "soon"}}
        ),
    )
    beyond_default = _reviewed_run(
        repo, home, "reviewed-past-a-day", now=now, age=timedelta(hours=25)
    )
    inside_default = _reviewed_run(
        repo, home, "reviewed-inside-a-day", now=now, age=timedelta(hours=2)
    )

    assert gc_module._run_directory_grace_seconds(PROJECT, repo) == 24 * 3600

    report = routing.garbage_collect(repo=repo, project=PROJECT, now=now)

    assert _pruned_ids(report) == {"reviewed-past-a-day"}, sorted(_pruned_ids(report))
    assert inside_default.exists()
    assert beyond_default.exists()


def test_a_dry_run_offers_only_the_reviewed_run_past_its_window(
    tmp_path: Path, monkeypatch
) -> None:
    home = tmp_path / "config"
    monkeypatch.setenv("RECKON_HOME", str(home))
    repo = repository(tmp_path)
    now = datetime(2026, 9, 1, tzinfo=UTC)
    dirs = plant_seven_states(repo, home, now)
    write_pointer(home, "live-run", phase="working")
    write_pointer(home, "queued-run", phase="queued")

    report = routing.garbage_collect(repo=repo, project=PROJECT, now=now)

    assert report["dry_run"] is True
    assert _pruned_ids(report) == {"reviewed-old-run"}, sorted(_pruned_ids(report))
    # Nothing was touched by the dry run.
    for name, path in dirs.items():
        assert path.exists(), name


def test_the_real_run_removes_exactly_that_one(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    home = tmp_path / "config"
    monkeypatch.setenv("RECKON_HOME", str(home))
    repo = repository(tmp_path)
    now = datetime(2026, 9, 1, tzinfo=UTC)
    dirs = plant_seven_states(repo, home, now)
    write_pointer(home, "live-run", phase="working")
    write_pointer(home, "queued-run", phase="queued")

    report = routing.garbage_collect(repo=repo, project=PROJECT, now=now, apply=True)

    assert _pruned_ids(report) == {"reviewed-old-run"}, sorted(_pruned_ids(report))
    assert not dirs["reviewed-old-run"].exists()
    for name in (
        "live-run",
        "queued-run",
        "unpromoted-run",
        "review-owed-run",
        "figures-missing-run",
        "reviewed-recent-run",
    ):
        assert dirs[name].exists(), name

    # Each removal prints the path it removed and the size it measured, before
    # the removal happens.
    out = capsys.readouterr().out
    assert str(dirs["reviewed-old-run"]) in out
    assert "bytes" in out

    pruned_row = next(
        row for row in report["run_directories"] if row["run_id"] == "reviewed-old-run"
    )
    assert pruned_row["removed"] is True
    assert pruned_row["bytes"] > 0


def test_the_withheld_states_say_why_they_are_kept(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "config"
    monkeypatch.setenv("RECKON_HOME", str(home))
    repo = repository(tmp_path)
    now = datetime(2026, 9, 1, tzinfo=UTC)
    plant_seven_states(repo, home, now)
    write_pointer(home, "live-run", phase="working")
    write_pointer(home, "queued-run", phase="queued")

    report = routing.garbage_collect(repo=repo, project=PROJECT, now=now)
    rows = {row["run_id"]: row for row in report["run_directories"]}

    # A review is owed for the promoted run whose review has not landed, and the
    # flow figures that were never derived still hold their run's directory.
    assert rows["review-owed-run"]["withheld"] == "review-owed"
    assert rows["figures-missing-run"]["withheld"] == "missing-derived-figure"

    # A live pointer and a queued entry are runs still in flight or waiting to
    # launch, so neither server is offered for removal at all.
    assert "live-run" not in _pruned_ids(report)
    assert "queued-run" not in _pruned_ids(report)
    # An unpromoted run has no terminal ledger row and may still be resumed.
    assert "unpromoted-run" not in _pruned_ids(report)
