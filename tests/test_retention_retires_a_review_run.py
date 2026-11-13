"""Ledgered reviewers retire without owing a review of themselves."""

from __future__ import annotations

import json
import os
import shlex
from datetime import UTC, datetime, timedelta

import pytest

from reckon import ledger
from reckon.crew import gc


@pytest.fixture
def review_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    root = tmp_path / "repo"
    state = root / "docs" / "state" / "sample"
    state.mkdir(parents=True)
    (state / "index.json").write_text("{}\n")
    home.mkdir()
    (home / "mounts.json").write_text(
        json.dumps({"mounts": {"sample": str(root / "docs")}})
    )
    (home / "flight.yaml").write_text("gc:\n  directory_grace: 168h\n")
    monkeypatch.setenv("RECKON_HOME", str(home))
    monkeypatch.setenv("RECKON_STATE_ROOT", str(root / "docs" / "state"))
    monkeypatch.setenv("RECKON_RUN_STORE", str(home / "crew" / "run_store.db"))
    assert gc._run_directory_grace_seconds("sample", root) == 7 * 24 * 3600
    return home, root, datetime(2026, 11, 1, tzinfo=UTC)


def plant_run(review_home, *, age, role="review", node="review-of-subject"):
    home, root, now = review_home
    run_id = "r-reviewer"
    directory = home / "crew" / "runs" / run_id
    directory.mkdir(parents=True)
    (directory / "stream.jsonl").write_text("{}\n")
    record = ledger.build_record(
        run_id=run_id,
        project="sample",
        plan="sample-plan",
        node=node,
        role=role,
        gate="not-run",
        manifest_path=str(directory / "manifest.md"),
    )
    record.update(
        review_tier="full", orientation_input_tokens=128_000.0, tool_steps=7.0
    )
    ledger.append_run("sample", record, root=root, allow_create=True)
    stored = root / "docs/state/sample/reviews/run/subject" / f"{run_id}.json"
    stored.parent.mkdir(parents=True)
    stored.write_text(
        json.dumps(
            {
                "review_run_id": run_id,
                "reviewed_run_id": "subject",
                "reviewed_base_sha": "a" * 40,
                "reviewed_head_sha": "b" * 40,
                "status": "parsed",
                "findings": [],
            }
        )
    )
    stamp = (now - age).timestamp()
    os.utime(directory, (stamp, stamp))
    return directory, stored


@pytest.mark.parametrize(
    ("role", "node"),
    [("review", "renamed-reviewer"), ("", "review-of-subject")],
)
def test_a_stored_review_past_its_window_is_retired(review_home, role, node):
    directory, stored = plant_run(
        review_home, age=timedelta(days=8), role=role, node=node
    )
    assert json.loads(stored.read_text())["review_run_id"] == directory.name

    report = gc.retention_sweep(apply=True, now=review_home[2], limit=5)

    assert report["removed"] == [directory.name], report
    assert not directory.exists()
    assert stored.is_file()


def test_a_stored_review_inside_its_window_is_kept(review_home):
    directory, stored = plant_run(review_home, age=timedelta(days=6))

    report = gc.retention_sweep(apply=True, now=review_home[2], limit=5)

    assert report["removed"] == []
    assert report["withheld"] == []
    assert directory.is_dir() and stored.is_file()


@pytest.mark.parametrize("role", ["implement", ""])
def test_an_implementation_owing_its_review_is_kept(review_home, role):
    directory, _ = plant_run(
        review_home, age=timedelta(days=8), role=role, node="implementation"
    )

    report = gc.retention_sweep(apply=True, now=review_home[2], limit=5)

    assert report["removed"] == []
    assert directory.is_dir()
    assert len(report["withheld"]) == 1
    row = report["withheld"][0]
    assert row["withheld"] == "review-owed"
    assert row["reason"] == (
        "a review of this run is required and its "
        "committed record is not yet stored, so the "
        "directory still holds the evidence the review "
        "reads; it stays until the review lands"
    )


def test_install_timer_writes_a_bounded_retention_sweep(tmp_path, monkeypatch):
    units = tmp_path / "units"
    install = gc.install_retention_timer
    commands = []
    monkeypatch.setattr(
        gc,
        "install_retention_timer",
        lambda: install(directory=units, run=commands.append),
    )

    assert gc.main(["--install-timer"]) == 0
    body = (units / gc.RETENTION_SERVICE_NAME).read_text()
    command = next(
        line.removeprefix("ExecStart=")
        for line in body.splitlines()
        if line.startswith("ExecStart=")
    )
    argv = shlex.split(command)
    assert argv[1:3] == ["-m", "reckon.crew.gc"]
    assert "--apply" in argv
    assert "--limit" in argv
    limit = int(argv[argv.index("--limit") + 1])
    assert limit == gc.RETENTION_SWEEP_LIMIT
    calls = []
    monkeypatch.setattr(
        gc, "retention_sweep", lambda **kwargs: calls.append(kwargs) or {}
    )
    assert gc.main(argv[3:]) == 0
    assert calls[0]["apply"] is True
    assert calls[0]["limit"] == limit
    assert commands == [["daemon-reload"], ["enable", "--now", gc.RETENTION_TIMER_NAME]]
