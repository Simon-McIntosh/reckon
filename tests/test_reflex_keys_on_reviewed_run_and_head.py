"""The review reflex recognises a review by the reviewed run and its head.

Three measured shapes motivated these cases. A review hand-dispatched under a
coordinator's own node id ran beside the reflex's review of the same run, on
identical store paths, because recognition keyed on the minted node id. A
review standing at an earlier revision kept a resumed run from ever receiving
its review at the new head, because recognition keyed once per run and never
per revision. And the composed done-when did not name the added-failure
derivation the stored record is read with.

The key is therefore a pair — the reviewed run and the revision the review
read — and every assertion drives ``recovery.dispatch_awaiting_reviews``:
composition, recognition and the idle sweep are all observed through the entry
point a coordinator's sweep calls, never through a helper.

Each fixture project lives under a throwaway configuration home, and the real
configuration home is asserted untouched after every test: an isolated read
does not prove an isolated write.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from reckon.crew import recovery, review, runs

PROJECT = "reflex-fixture"
SESSION = "coordinator-fixture"
SOURCE_RUN = "r-reflex-source"
HAND_REVIEW = "r-reflex-review-hand"
STALE_REVIEW = "r-reflex-review-old-head"

REAL_HOME = Path.home() / ".config" / "reckon"

CONFIG = {
    "default_backend": "alpha",
    "local_backend": "alpha",
    "backends": {
        "alpha": {
            "launch": "cli",
            "command": "codex",
            "model": "some-model",
            "effort": "high",
            "service": False,
            "sandbox": "worktree-full",
            "session_reuse": True,
            "time_budget": "25m",
        }
    },
    "roles": {"implement": {}, "review": {}},
    "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
}


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _seed_repository(home: Path, root: Path) -> None:
    """One plan document, one commit, and the mounts pointing the project at it."""
    (root / "docs" / "plans").mkdir(parents=True)
    (root / "docs" / "plans" / "fixture.html").write_text(
        f'<meta name="docs-project" content="{PROJECT}">'
        '<meta name="reckon-type" content="plan">'
        '<meta name="plan-slug" content="fixture">'
        '<h2 id="s2">A sweep dispatches the composed review</h2>',
        encoding="utf-8",
    )
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "seed.txt", "docs/plans/fixture.html"),
        ("commit", "-q", "-m", "chore: seed"),
    ):
        _git(root, *arguments)
    (home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )


class _Fleet:
    """One throwaway project: its configuration home, repository and launches.

    The configuration home moves between fixtures, so every helper begins by
    pointing the environment at this fleet's own home: a helper that inherited
    the previous fleet's home would write its state somewhere the sweep does
    not read.
    """

    def __init__(self, home, repo, launches, launcher, monkeypatch):
        self.home = home
        self.repo = repo
        self.launches = launches
        self._launcher = launcher
        self._monkeypatch = monkeypatch

    def use(self) -> None:
        self._monkeypatch.setenv("RECKON_HOME", str(self.home))

    def head(self) -> str:
        return _git(self.repo, "rev-parse", "HEAD")

    def commit(self, name: str) -> str:
        (self.repo / f"{name}.txt").write_text(f"{name}\n", encoding="utf-8")
        _git(self.repo, "add", f"{name}.txt")
        _git(self.repo, "commit", "-q", "-m", f"chore: add {name}")
        return self.head()

    def sweep(self) -> dict:
        self.use()
        with runs.follower_claim(PROJECT, SESSION, delivery="stream"):
            return recovery.dispatch_awaiting_reviews(
                project=PROJECT, config=CONFIG, launcher=self._launcher
            )


@pytest.fixture()
def fleets(tmp_path, monkeypatch):
    """Build throwaway fleets; each launch is recorded as the plan it ran."""
    dispatcher = importlib.import_module("reckon.crew.dispatch")

    def prepare(_repo, session: str, node: str, base: str) -> dict:
        path = Path(str(_repo)).parent / "worktrees" / f"{session}-{node}"
        path.mkdir(parents=True, exist_ok=True)
        return {
            "path": str(path),
            "base": base,
            "base_sha": _git(Path(str(_repo)), "rev-parse", "HEAD"),
        }

    monkeypatch.setattr(dispatcher, "_create_worktree", prepare)

    def build(name: str) -> _Fleet:
        base = tmp_path / name
        home = base / "config"
        root = base / "repo"
        home.mkdir(parents=True)
        _seed_repository(home, root)
        launches: list = []

        def launcher(plan, *, log_path, stderr_path, prompt_path):
            launches.append(plan)
            return os.getpid()

        fleet = _Fleet(home, root, launches, launcher, monkeypatch)
        fleet.use()
        return fleet

    return build


@pytest.fixture(autouse=True)
def real_config_home_untouched():
    """No fixture write reaches the real configuration home.

    The fixture project's name exists nowhere else, so a pointer or a review
    record naming it under the real home is this run's leak. An isolated read
    does not prove an isolated write, which is why the write direction is
    checked rather than the reads being trusted.
    """
    yield
    reviews = REAL_HOME / "crew" / "reviews" / PROJECT
    assert not reviews.exists(), f"fixture review store leaked to {reviews}"
    live = REAL_HOME / "crew" / "live"
    leaked = []
    if live.is_dir():
        for path in live.glob("*.json"):
            text = path.read_text(encoding="utf-8", errors="replace")
            if PROJECT in text:
                leaked.append(str(path))
    assert not leaked, f"fixture pointers leaked to the real live directory: {leaked}"


def _scoring_run(fleet: _Fleet, run_id: str = SOURCE_RUN) -> dict:
    """A completed run awaiting review: live pointer plus the manifest it needs."""
    fleet.use()
    manifest = fleet.home / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        f"node: {run_id}\nstatus: complete\ncommits: {run_id}\n", encoding="utf-8"
    )
    record = {
        "run_id": run_id,
        "project": PROJECT,
        "session": SESSION,
        "process_alive": False,
        "repo": str(fleet.repo),
        "worktree": str(fleet.repo),
        "backend": "alpha",
        "launch": "cli",
        "argv": ["codex"],
        "phase": "starting",
        "node": {"id": "source-node", "plan": "fixture", "section": "s2"},
        "manifest_path": str(manifest),
    }
    runs._write_json(runs.pointer_path(run_id), record)
    return record


def _live_review(
    fleet: _Fleet, run_id: str, *, node_id: str, reviewed: str, heads: list[str]
) -> None:
    """A live review granted exactly the store paths the dispatch composes."""
    fleet.use()
    granted = [str(review.review_path(PROJECT, reviewed))]
    granted.extend(
        str(review.review_path(PROJECT, reviewed, reviewed_head_sha=head))
        for head in heads
    )
    runs._write_json(
        runs.pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "session": SESSION,
            "process_alive": True,
            "role": "review",
            "backend": "alpha",
            "node": {
                "id": node_id,
                "plan": "fixture",
                "section": "s2",
                "write_paths": granted,
            },
        },
    )


def _stored_review(fleet: _Fleet, reviewed: str, head: str) -> None:
    fleet.use()
    review.store_review(
        {
            "project": PROJECT,
            "reviewed_run_id": reviewed,
            "status": "parsed",
            "timestamp": "2026-09-26T12:00:00+00:00",
            "reviewed_base_sha": head,
            "reviewed_head_sha": head,
            "scores": dict.fromkeys(review.REVIEW_DIMENSIONS, 10),
            "total": 50,
        }
    )


def _source_report(result: dict) -> dict:
    rows = [row for row in result["reports"] if row.get("run_id") == SOURCE_RUN]
    assert len(rows) == 1, result["reports"]
    return rows[0]


def test_a_live_review_under_any_node_id_suppresses_the_reflex(fleets) -> None:
    """A review of this run at this head stands, whatever the reviewer is called."""
    fleet = fleets("live_review")
    head = fleet.head()
    _scoring_run(fleet)
    _live_review(
        fleet,
        HAND_REVIEW,
        node_id="re-review-of-source-node",
        reviewed=SOURCE_RUN,
        heads=[head],
    )

    result = fleet.sweep()

    assert result["dispatched"] == []
    assert fleet.launches == []
    report = _source_report(result)
    assert report.get("dispatched") is False
    assert "in flight" in str(report.get("reason"))


def test_a_stored_record_for_the_head_stands_and_an_older_head_does_not(
    fleets,
) -> None:
    """The stored half: same head stands, and the pair key reads the revision.

    The stale half is the discriminating negative: without it, a head-blind
    reader that suppressed on any stored record would pass the first half.
    """
    fleet = fleets("stored_current")
    head = fleet.head()
    _scoring_run(fleet)
    _stored_review(fleet, SOURCE_RUN, head)

    result = fleet.sweep()

    assert result["dispatched"] == []
    assert fleet.launches == []

    stale = fleets("stored_older")
    old_head = stale.head()
    _scoring_run(stale)
    _stored_review(stale, SOURCE_RUN, old_head)
    stale.commit("second")

    resumed = stale.sweep()

    assert len(resumed["dispatched"]) == 1
    assert len(stale.launches) == 1


def test_a_run_resumed_to_a_new_head_gets_one_review_and_a_second_sweep_none(
    fleets,
) -> None:
    """A review standing at the old head must not starve the new one for long."""
    fleet = fleets("resumed")
    old_head = fleet.head()
    _scoring_run(fleet)
    _live_review(
        fleet,
        STALE_REVIEW,
        node_id="re-review-of-source-node",
        reviewed=SOURCE_RUN,
        heads=[old_head],
    )
    new_head = fleet.commit("second")

    first = fleet.sweep()

    assert len(fleet.launches) == 1, first
    assert len(first["dispatched"]) == 1
    brief = fleet.launches[0].stdin_text
    assert (
        str(review.review_path(PROJECT, SOURCE_RUN, reviewed_head_sha=new_head))
        in brief
    )
    assert (
        str(review.review_path(PROJECT, SOURCE_RUN, reviewed_head_sha=old_head))
        not in brief
    )

    second = fleet.sweep()

    assert second["dispatched"] == []
    assert len(fleet.launches) == 1


def test_the_composed_done_when_names_the_added_failure_derivation(fleets) -> None:
    """The brief names the count the record is read with and where it comes from."""
    fleet = fleets("done_when")
    _scoring_run(fleet)

    result = fleet.sweep()

    assert len(result["dispatched"]) == 1
    assert len(fleet.launches) == 1
    brief = fleet.launches[0].stdin_text
    assert SOURCE_RUN in brief
    assert "added_failure_count" in brief
    assert "added_failure_ids" in brief
    assert "baseline_suite" in brief
    assert "after_suite" in brief
