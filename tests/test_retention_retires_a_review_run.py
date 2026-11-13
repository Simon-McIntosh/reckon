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


@pytest.mark.parametrize(
    "damage",
    [
        "missing",
        "invalid-json",
        "invalid-utf8",
        "wrong-reviewer",
        "unknown-status",
        "no-review",
    ],
)
def test_an_old_reviewer_without_its_readable_result_is_kept(review_home, damage):
    directory, stored = plant_run(review_home, age=timedelta(days=8))
    if damage == "missing":
        stored.unlink()
    elif damage == "invalid-json":
        stored.write_text("{")
    elif damage == "invalid-utf8":
        stored.write_bytes(bytes([255]))
    else:
        payload = json.loads(stored.read_text())
        if damage == "wrong-reviewer":
            payload["review_run_id"] = "r-someone-else"
        elif damage == "unknown-status":
            payload["status"] = "unreadable-verdict"
        else:
            payload = {"review_run_id": directory.name, "status": "parsed"}
        stored.write_text(json.dumps(payload))

    report = gc.retention_sweep(apply=True, now=review_home[2], limit=5)

    assert report["removed"] == [], report
    assert directory.is_dir()
    row = report["withheld"][0]
    assert row["withheld"] == "missing-review-result"
    assert "review record produced by this reviewer" in row["reason"]


@pytest.mark.parametrize("location", ["staging", "plan"])
def test_a_review_result_can_be_stored_outside_the_run_review_tree(
    review_home, location
):
    directory, stored = plant_run(review_home, age=timedelta(days=8))
    _home, root, now = review_home
    payload = json.loads(stored.read_text())
    if location == "staging":
        from reckon.crew import review

        target = review.review_store_root() / "sample" / "stored-review.json"
    else:
        target = root / "docs/state/sample/reviews/plan/example" / stored.name
        payload.pop("reviewed_run_id")
        payload.update(plan_slug="example", plan_version=1)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload))
    stored.unlink()

    report = gc.retention_sweep(apply=True, now=now, limit=5)

    assert report["removed"] == [directory.name], report
    assert not directory.exists()
    assert target.is_file()


@pytest.mark.parametrize("result_present", [True, False])
def test_a_committed_only_figure_result_controls_reviewer_retention(
    review_home, result_present
):
    from reckon.crew import review, review_records

    directory, bare = plant_run(review_home, age=timedelta(days=8))
    _home, root, now = review_home
    payload = json.loads(bare.read_text())
    payload.update(project="sample", rubric=review.FIGURE_REVIEW_RUBRIC)
    stored = review_records.store_committed_review(payload, root=root)
    assert stored != bare
    assert stored == review.review_path(
        "sample",
        "subject",
        committed_root=review.committed_review_root("sample", root=root),
        review_run_id=directory.name,
        reviewed_head_sha=payload["reviewed_head_sha"],
        rubric=review.FIGURE_REVIEW_RUBRIC,
    )
    bare.unlink()
    if not result_present:
        stored.unlink()
    (stored.parent / "unreadable.json").write_bytes(bytes([255]))
    assert review_records.record_for_review_run("sample", directory.name) is None

    report = gc.retention_sweep(apply=True, now=now, limit=5)

    if result_present:
        assert report["removed"] == [directory.name], (stored, report)
        assert not directory.exists()
        assert stored.is_file()
    else:
        assert report["removed"] == []
        assert directory.is_dir()
        assert report["withheld"][0]["withheld"] == "missing-review-result"
