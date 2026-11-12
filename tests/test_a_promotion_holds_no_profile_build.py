"""A promotion folds its row into the profile with no ledger read.

A promotion holds the promotion lock while it lands a run, and the profile
refresh that runs there used to pay for a whole-ledger read: it decoded the
index and, when the index read as stale during a landing burst, scanned every
authoritative run document. A sweep pays that once per review, so each
promotion's duration grew with the ledger.

The promotion already holds the row it wrote and can see the ledger state it
extended, so the refresh folds the stored samples plus that row and reads
nothing else. The cases below drive ``crew complete`` for a review run against
a 200-row ledger, once with a fresh index and once with an index the header
read reports stale, and count the profile path's ledger decodes while the
promotion lock is held. A pick made afterwards serves a profile holding all
three promoted rows.
"""

from __future__ import annotations

import json
import sys
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reckon import _plan_html, _store, ledger
from reckon.crew import promotion
from reckon.crew.picker import lane_context
from reckon.crew.runs import _write_json, crew_home, pointer_path, run_dir
from tests.helpers.git import git as _git

PROJECT = "profile-promotion-fixture"
PLAN = "profile-promotion-target"
REVIEWED_RUN_ID = "r-20261112T090000000000-reviewed-run"
SEEDED_ROWS = 200


def _built_at() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _seed_row(index: int, now: datetime, **fields: object) -> dict:
    return {
        "run_id": f"seed-run-{index}",
        "backend": "seed-backend",
        "agent": {"effort": "standard"},
        "role": "implement",
        "spec_level": "guided",
        "completed_at": (now - timedelta(minutes=index + 1)).isoformat(),
        "wall_seconds": 300 + index,
        "time_budget": "30m",
        "gate": "passed",
        "budget": {"tokens": {"output_tokens": 100 + index}},
        **fields,
    }


def _write_resource(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    bare = (
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{PROJECT}">'
        f"<title>{state['slug']}</title>"
        '</head><body><main class="plan-doc"></main></body></html>\n'
    )
    path.write_text(_plan_html.write_state(bare, state), encoding="utf-8")


@pytest.fixture()
def repository(isolated_reckon_home: Path, tmp_path: Path) -> Path:
    """A committed repository whose docs directory is the project's mount."""
    assert crew_home().is_relative_to(isolated_reckon_home)
    root = tmp_path / "repo"
    _write_resource(
        root / "docs" / "plans" / f"{PLAN}.html",
        {
            "type": "plan",
            "slug": PLAN,
            "title": "Profile Promotion Target",
            "status": "active",
            "version": 0,
            "comments": {},
        },
    )
    (root / "docs" / "state" / PROJECT).mkdir(parents=True, exist_ok=True)
    # The config home's state root is a symlink into the registered checkout, so
    # ``ledger_path(project)`` and ``ledger_path(project, repo_root)`` are one
    # file, as they are on a real home. Without it the fixture would hold two
    # ledgers and the promotion would fold into the wrong one.
    state_root = _store._state_root()
    state_root.parent.mkdir(parents=True, exist_ok=True)
    if not state_root.exists():
        state_root.symlink_to(root / "docs" / "state")
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
    ):
        _git(root, *arguments)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(root, "add", "seed.txt", "docs")
    _git(root, "commit", "-q", "-m", "chore: seed repository")
    (isolated_reckon_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


@pytest.fixture()
def seeded_ledger(isolated_reckon_home: Path, repository: Path) -> datetime:
    """A 200-row ledger beside a published profile for the project."""
    now = _built_at()
    path = ledger.ledger_path(PROJECT)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "data": {
                    "_version": 1,
                    "members": [],
                    "holds": [],
                    "runs": [_seed_row(index, now) for index in range(SEEDED_ROWS)],
                }
            }
        ),
        encoding="utf-8",
    )
    ledger._bump_generation(PROJECT)
    # Publish the profile a later promotion folds its row into. This is the
    # refresh that runs outside a promotion: it is what a warm home already has.
    profile = lane_context.refresh_run_time_profile(PROJECT, now=now)
    assert profile is not None and profile["rows"] == SEEDED_ROWS
    lane_context._PROFILE_CACHE.clear()
    return now


def _build_review_run(tmp_path: Path, repository: Path, sequence: int) -> str:
    """A completed review run whose promotion is the case under test."""
    run_id = f"r-{20261112 + sequence}T090{sequence}00000000-review-run-{sequence}"
    _write_json(
        pointer_path(REVIEWED_RUN_ID),
        {"run_id": REVIEWED_RUN_ID, "project": PROJECT, "role": "implement"},
    )
    manifest = tmp_path / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        f"node: review-run-{sequence}\n"
        "status: complete\n"
        "checkpoint: the review is delivered\n",
        encoding="utf-8",
    )
    _write_json(
        pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "repo": str(repository),
            "worktree": str(repository),
            "launch": "in-harness",
            "role": "review",
            "backend": f"promoted-backend-{sequence}",
            "created_at": "2026-11-12T09:00:00Z",
            "manifest_path": str(manifest),
            "node": {
                "id": f"review-run-{sequence}",
                "plan": PLAN,
                "section": "s21",
                "time_budget": "25m",
                "role": "review",
                "review_tier": "content",
                "write_paths": [],
            },
        },
    )
    directory = Path(run_dir(run_id))
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "exit.json").write_text(
        json.dumps({"run_id": run_id, "exit_code": 0}), encoding="utf-8"
    )
    return run_id


def _in_profile_path() -> bool:
    """Whether the running call was made by the profile refresh."""
    frame = sys._getframe(1)
    while frame is not None:
        if frame.f_globals.get("__name__") == lane_context.__name__ and (
            frame.f_code.co_name
            in {"refresh_run_time_profile", "_promotion_fold_inputs"}
        ):
            return True
        frame = frame.f_back
    return False


@contextmanager
def _profile_decode_counter(monkeypatch):
    """Count index decodes and document merges made by the profile path.

    Calls are attributed by the stack rather than by the lock alone: the
    promotion reads the ledger itself while it holds the lock, and that read is
    not the profile refresh this case is about.
    """
    lock_held = {"now": False}
    real_lock = promotion._promotion_lock

    @contextmanager
    def tracked(run_id):
        with real_lock(run_id):
            lock_held["now"] = True
            try:
                yield
            finally:
                lock_held["now"] = False

    monkeypatch.setattr(promotion, "_promotion_lock", tracked)
    calls: list[dict[str, object]] = []

    def counting(name, original):
        def wrapper(*args, **kwargs):
            calls.append(
                {
                    "name": name,
                    "in_lock": lock_held["now"],
                    "in_profile": _in_profile_path(),
                }
            )
            return original(*args, **kwargs)

        return wrapper

    for name in ("_index_payload", "_last_good_index_data", "_merge_run_documents"):
        monkeypatch.setattr(ledger, name, counting(name, getattr(ledger, name)))
    yield calls


def _promote(run_id: str, repository: Path) -> dict:
    return promotion.complete(
        run_id,
        gate="not-run",
        root=repository,
        outcome="the review evidence could not be produced in this fixture",
        no_commit="the fixture writes only its ledger, which is not a code change",
    )


@pytest.mark.parametrize("stale_index", [False, True])
def test_a_promotion_folds_its_row_without_a_ledger_decode(
    seeded_ledger, repository, tmp_path, monkeypatch, stale_index
):
    now = seeded_ledger
    if stale_index:
        # An append that leaves the index behind, and an index refresh the
        # reader refuses: the header read then reports index_age_seconds.
        monkeypatch.setattr(ledger, "_index_record_written_document", lambda *a: None)
        refused = ledger._indexed_data

        def _maybe_refused(*args, **kwargs):
            if _in_profile_path():
                raise OSError("fixture index refresh refused during promotion")
            return refused(*args, **kwargs)

        monkeypatch.setattr(ledger, "_indexed_data", _maybe_refused)

    run_ids = [
        _build_review_run(tmp_path, repository, sequence)
        for sequence in range(1, 4)
    ]
    with _profile_decode_counter(monkeypatch) as calls:
        for run_id in run_ids:
            _promote(run_id, repository)

    profile_calls = [
        call for call in calls if call["in_lock"] and call["in_profile"]
    ]
    print(
        f"stale_index={stale_index} profile_calls={profile_calls} "
        f"all_calls={len(calls)}"
    )
    assert profile_calls == [], (
        "the profile refresh decoded the ledger while the promotion lock was held: "
        f"{profile_calls}"
    )
    assert calls, "the instrument must see the promotion's own ledger reads"

    lane_context._PROFILE_CACHE.clear()
    served, age = lane_context._cached_run_time_profile(PROJECT, now=now)
    print(f"served_rows={served.get('rows')} groups={len(served.get('groups', []))}")
    assert served["rows"] == SEEDED_ROWS + len(run_ids)
    assert {
        group["backend"] for group in served["groups"]
    } >= {f"promoted-backend-{sequence}" for sequence in range(1, 4)}