"""Census and sampler behaviour over a synthesised stream, run directory and crew home.

The census reclassifies every event through ``ticker.stream_carries`` and
``ticker._pane_word``, so the fixtures are events shaped as the producer writes
them. The three halted kinds cannot be read from a stream row alone, so the
fixtures place the ``attempt-N.claim`` mtimes and the ``status-repair.json``
that decide them beside a synthetic run directory.

The sampler is exercised against a synthesised crew home: its snapshots stand
where the producer publishes them and its live pointers where the fleet keeps
them, so no arm reads or writes the operator's own home.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from reckon.crew import review as review_module

WINDOW_START = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)
WINDOW_END = datetime(2026, 10, 23, 13, 0, tzinfo=UTC)


def _load(name: str):
    """Load a sibling census script by path; ``scripts/`` is not a package."""
    path = Path(__file__).resolve().parent / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"census_{name}", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


monitor_census = _load("monitor_census")


def _iso(instant: datetime) -> str:
    return instant.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _event(run_id: str, from_state: str, to_state: str, at: datetime, **extra):
    """One transition event shaped as the producer writes it."""
    event = {
        "run_id": run_id,
        "project": "proj",
        "event": "transition",
        "from_state": from_state,
        "to_state": to_state,
        "recovery_classification": to_state,
        "observed_at": _iso(at),
        "node": run_id.rsplit("-", 1)[-1],
    }
    event.update(extra)
    return event


def _write_stream(watch: Path, rows) -> None:
    watch.mkdir(parents=True, exist_ok=True)
    with (watch / "proj-abcd1234.events").open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")


def _run_directory(
    root: Path,
    run_id: str,
    *,
    claims: tuple[tuple[str, datetime], ...] = (),
    repaired_at: datetime | None = None,
) -> None:
    directory = root / run_id
    directory.mkdir(parents=True, exist_ok=True)
    for name, at in claims:
        claim = directory / name
        claim.write_text("{}", encoding="utf-8")
        os.utime(claim, (at.timestamp(), at.timestamp()))
    if repaired_at is not None:
        (directory / "status-repair.json").write_text(
            json.dumps({"run_id": run_id, "repaired_at": _iso(repaired_at)}),
            encoding="utf-8",
        )


def test_census_counts_one_of_each_kind(tmp_path: Path) -> None:
    """One synthetic row of each counted kind gives a count of one each."""
    watch = tmp_path / "watch"
    root = tmp_path / "runs"
    alive_run = "r-20261023T120000000001-alive"
    progress_run = "r-20261023T120000000002-progress"
    repair_run = "r-20261023T120000000003-repair"
    wait_run = "r-20261023T120000000004-wait"
    lift_run = "r-20261023T120000000005-lift"

    base = WINDOW_START + timedelta(minutes=10)
    halt_alive = base
    halt_progress = base + timedelta(minutes=5)
    progress_at = halt_progress + timedelta(seconds=103)
    halt_repair = base + timedelta(minutes=12)

    _write_stream(
        watch,
        [
            # Kind one: the row's own event records the worker alive.
            _event(alive_run, "working", "blocked", halt_alive, process_alive=True),
            # Kind two: a later progress row follows with no claim between.
            _event(progress_run, "working", "blocked", halt_progress),
            _event(progress_run, "blocked", "completed_unpromoted", progress_at),
            # Kind three: the row follows a status repair.
            _event(repair_run, "complete", "blocked", halt_repair),
            # A wait returning from its aging row shares the pane word, so the
            # row draws as ``waiting -> waiting``.
            _event(wait_run, "wait-aged", "waiting", base + timedelta(minutes=25)),
            # One lift id, granted then ended.
            _event(
                lift_run,
                "lift-in-force",
                "lift-granted",
                base + timedelta(minutes=30),
                lift_id="lift-1",
            ),
            _event(
                lift_run,
                "lift-in-force",
                "lift-ended",
                base + timedelta(minutes=40),
                lift_id="lift-1",
            ),
        ],
    )
    # The claim belongs to an attempt that ran before the halted row, so it is
    # not between and the row still counts; the repair arm's claim sits in the
    # halted row's own second, which cannot have produced the halt.
    _run_directory(
        root,
        progress_run,
        claims=(("attempt-1.claim", halt_progress - timedelta(minutes=4)),),
    )
    _run_directory(
        root,
        repair_run,
        claims=(("attempt-4.claim", halt_repair + timedelta(seconds=0.398)),),
        repaired_at=halt_repair - timedelta(seconds=19),
    )

    census = monitor_census.census_streams(
        since=WINDOW_START, until=WINDOW_END, watch_dir=watch, runs_root=root
    )

    assert census["events"] == 7
    assert census["halted_rows"] == 3
    assert census["carried_rows"]["proj:halted"] == 3
    kinds = census["kinds"]
    assert kinds["process_alive"]["count"] == 1
    assert kinds["process_alive"]["by_transition"] == {"working->blocked": 1}
    assert kinds["returns_to_progress"]["count"] == 1
    assert kinds["returns_to_progress"]["rows"][0]["run_id"] == progress_run
    assert kinds["after_status_repair"]["count"] == 1
    assert kinds["after_status_repair"]["rows"][0]["run_id"] == repair_run
    assert census["same_word_wait_rows"]["count"] == 1
    assert census["same_word_wait_rows"]["rows"][0]["run_id"] == wait_run
    assert census["lift_transitions"] == {
        "lift-1": {"granted": 1, "ended": 1, "in_force": 0}
    }


def test_claim_between_is_compared_at_second_granularity(tmp_path: Path) -> None:
    """A claim in either bound's own second is not between, so the row counts."""
    watch = tmp_path / "watch"
    root = tmp_path / "runs"
    halt = WINDOW_START + timedelta(minutes=10)
    progress_at = halt + timedelta(minutes=2)

    same_second_as_halt = "r-20261023T120000000010-halt-second"
    same_second_as_progress = "r-20261023T120000000011-progress-second"
    strictly_between = "r-20261023T120000000012-between"

    rows = []
    for run_id in (same_second_as_halt, same_second_as_progress, strictly_between):
        rows.append(_event(run_id, "working", "blocked", halt))
        rows.append(_event(run_id, "blocked", "completed_unpromoted", progress_at))
    _write_stream(watch, rows)

    _run_directory(
        root,
        same_second_as_halt,
        claims=(("attempt-2.claim", halt + timedelta(seconds=0.4)),),
    )
    _run_directory(
        root,
        same_second_as_progress,
        claims=(("attempt-2.claim", progress_at + timedelta(seconds=0.9)),),
    )
    _run_directory(
        root,
        strictly_between,
        claims=(("attempt-2.claim", halt + timedelta(seconds=30)),),
    )

    census = monitor_census.census_streams(
        since=WINDOW_START, until=WINDOW_END, watch_dir=watch, runs_root=root
    )

    counted = {row["run_id"] for row in census["kinds"]["returns_to_progress"]["rows"]}
    assert census["kinds"]["returns_to_progress"]["count"] == 2
    assert counted == {same_second_as_halt, same_second_as_progress}


# ── Review-missing sampler ──────────────────────────────────────────────────

REVIEWED_RUN = "r-20261023T120000000020-reviewed"
REVIEW_RUN = "r-20261023T120100000021-review"
HEAD = "a" * 40
OLDER_HEAD = "b" * 40
LAUNCH = datetime(2026, 10, 23, 12, 20, tzinfo=UTC)
AFTER_LAUNCH = LAUNCH + timedelta(minutes=2)
BEFORE_LAUNCH = LAUNCH - timedelta(minutes=2)


def _review_paths(tmp_path: Path, head: str) -> list[str]:
    """The record paths a review of the run at ``head`` declares."""
    run_directory = tmp_path / "crew" / "runs" / REVIEWED_RUN
    return review_module.run_review_record_paths(
        run_directory, REVIEWED_RUN, reviewed_head_sha=head
    )


def _build_home(
    tmp_path: Path,
    *,
    review_phase: str = "running",
    review_head: str | None = HEAD,
    write_review: bool = True,
    computed_at: datetime = AFTER_LAUNCH,
) -> Path:
    """A synthetic crew home: a snapshot, the reviewed run, and its review."""
    home = tmp_path / "crew"
    (home / "live").mkdir(parents=True, exist_ok=True)
    (home / "obligations" / "proj").mkdir(parents=True, exist_ok=True)

    worktree = tmp_path / "worktree"
    (worktree / ".git").mkdir(parents=True, exist_ok=True)
    (worktree / ".git" / "HEAD").write_text(HEAD + "\n", encoding="utf-8")

    reviewed = {
        "run_id": REVIEWED_RUN,
        "project": "proj",
        "phase": "delivered",
        "status": "complete",
        "worktree": str(worktree),
        "node": {"id": "node-reviewed"},
    }
    (home / "live" / f"{REVIEWED_RUN}.json").write_text(
        json.dumps(reviewed), encoding="utf-8"
    )

    if write_review:
        review = {
            "run_id": REVIEW_RUN,
            "project": "proj",
            "phase": review_phase,
            "status": review_phase,
            "role": "review",
            "launched_at": _iso(LAUNCH),
            "node": {
                "id": f"review-of-{REVIEWED_RUN}",
                "write_paths": _review_paths(tmp_path, review_head or HEAD),
            },
        }
        (home / "live" / f"{REVIEW_RUN}.json").write_text(
            json.dumps(review), encoding="utf-8"
        )

    snapshot = {
        "computed_at": _iso(computed_at),
        "obligations": [
            {"run_id": REVIEWED_RUN, "head": HEAD, "kind": "review-missing"}
        ],
    }
    (home / "obligations" / "proj" / "session-one.json").write_text(
        json.dumps(snapshot), encoding="utf-8"
    )
    return home


def _sample(tmp_path: Path, home: Path) -> tuple[dict, list[dict]]:
    samples = tmp_path / "samples.jsonl"
    counts = monitor_census.sample_once(home=home, samples_path=samples)
    entries = []
    if samples.exists():
        entries = [
            json.loads(line)
            for line in samples.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    return counts, entries


def test_sampler_counts_a_live_review_as_the_defect(
    tmp_path: Path, monkeypatch
) -> None:
    """One duty beside a live review counts once, copying both records."""
    monkeypatch.setenv("RECKON_HOME", str(tmp_path))
    home = _build_home(tmp_path)

    counts, entries = _sample(tmp_path, home)

    assert counts["review_missing_duties"] == 1
    assert counts["defect"] == 1
    assert counts["stale_snapshot"] == 0
    assert len(entries) == 1
    assert entries[0]["duty"]["run_id"] == REVIEWED_RUN
    assert entries[0]["duty"]["kind"] == "review-missing"
    assert entries[0]["review_pointer"]["run_id"] == REVIEW_RUN
    assert entries[0]["reading"] == "defect"


def test_sampler_counts_no_defect_without_a_review_pointer(
    tmp_path: Path, monkeypatch
) -> None:
    """A duty with no live review standing for it is not the defect."""
    monkeypatch.setenv("RECKON_HOME", str(tmp_path))
    home = _build_home(tmp_path, write_review=False)

    counts, _entries = _sample(tmp_path, home)

    assert counts["review_missing_duties"] == 1
    assert counts["defect"] == 0
    assert counts["live_review"] == 0


def test_sampler_counts_no_defect_for_a_queued_review(
    tmp_path: Path, monkeypatch
) -> None:
    """A review accepted into the queue is not yet working on the revision."""
    monkeypatch.setenv("RECKON_HOME", str(tmp_path))
    home = _build_home(tmp_path, review_phase="queued")

    counts, _entries = _sample(tmp_path, home)

    assert counts["defect"] == 0
    assert counts["live_review"] == 0


def test_sampler_counts_no_defect_for_a_review_of_an_older_head(
    tmp_path: Path, monkeypatch
) -> None:
    """A review granted the record for an older head does not stand for this one."""
    monkeypatch.setenv("RECKON_HOME", str(tmp_path))
    home = _build_home(tmp_path, review_head=OLDER_HEAD)

    counts, _entries = _sample(tmp_path, home)

    assert counts["defect"] == 0
    assert counts["live_review"] == 0


def test_sampler_counts_a_snapshot_computed_before_the_launch_as_stale(
    tmp_path: Path, monkeypatch
) -> None:
    """A snapshot older than the launch is stale, not the publisher's defect."""
    monkeypatch.setenv("RECKON_HOME", str(tmp_path))
    home = _build_home(tmp_path, computed_at=BEFORE_LAUNCH)

    counts, entries = _sample(tmp_path, home)

    assert counts["defect"] == 0
    assert counts["stale_snapshot"] == 1
    assert entries[0]["reading"] == "stale_snapshot"
