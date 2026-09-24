"""A completed review-role run is never the subject of another review.

The classifier reads a completed manifest with no attached review as
``scoring``, which is the classification the reflex acts on: it composes that
run's review dispatch and launches it. For a run whose own role is ``review``
that composition names the run as its own source, so the review completes,
classifies as scoring in turn, and dispatches another review of the same shape
— a chain that never drains, each link a real dispatch against a real member.

The exemption is keyed on the role the run carried, and these tests assert both
halves, because a guard that only ever quietens is indistinguishable from one
that has been deleted: a review-role run leaves ``scoring`` and its
``next_action`` composes no dispatch naming it, while an implement-role run
with no stored review still scores and still composes the review it lacks. The
role is the key rather than the absence of a review, so the exemption cannot be
reached by a run that simply never had one produced.
"""

from __future__ import annotations

import importlib
import os
import subprocess
import time
from pathlib import Path

import pytest

from reckon import crew
from reckon.crew import promotion, recovery, runs
from reckon.crew.dispatch import WATCHER_LOAD_BOUND_SECONDS
from reckon.crew.node import CrewError

# The reflex is gated by the watch admission, so the fleet tests arm the
# producer the gate reads rather than accepting a suite-wide waiver, which
# would let every dispatch through and prove nothing about the refusal.
pytestmark = pytest.mark.arms_watch_producer


CONFIG = {
    "default_backend": "alpha",
    "local_backend": "alpha",
    "backends": {
        "alpha": {
            "launch": "cli",
            "command": "codex",
            "model": "some-model",
            "effort": "high",
            "sandbox": "worktree-full",
            "session_reuse": True,
            "time_budget": "25m",
        }
    },
    "roles": {"implement": {}, "review": {}},
    "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
}


@pytest.fixture()
def isolated_project(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))

    repo = tmp_path / "repo"
    scripts = repo / "skills" / "reckon-build" / "scripts"
    scripts.mkdir(parents=True)
    source = (
        Path(__file__).parents[1]
        / "skills"
        / "reckon-build"
        / "scripts"
        / "worktree_fleet.py"
    )
    (scripts / "worktree_fleet.py").write_text(source.read_text(encoding="utf-8"))
    plans = repo / "docs" / "plans"
    plans.mkdir(parents=True)
    (plans / "fixture.html").write_text(
        '<meta name="docs-project" content="sample">'
        '<meta name="reckon-type" content="plan">'
        '<meta name="plan-slug" content="fixture">'
        '<h2 id="s2">A completed review run dispatches no review</h2>',
        encoding="utf-8",
    )
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "worker@example.invalid"],
        ["config", "user.name", "Worker"],
        ["add", "seed.txt", "skills", "docs/plans/fixture.html"],
        ["commit", "-q", "-m", "chore: seed"],
    ):
        subprocess.run(["git", *arguments], cwd=repo, check=True, capture_output=True)
    (config_home / "mounts.json").write_text(
        '{"sample": "' + str(repo / "docs") + '"}', encoding="utf-8"
    )

    dispatch_module = importlib.import_module("reckon.crew.dispatch")
    base_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    def prepare_worktree(_repo: Path, session: str, node: str, base: str) -> dict:
        path = tmp_path / "worktrees" / f"{session}-{node}"
        path.mkdir(parents=True, exist_ok=True)
        return {"path": str(path), "base": base, "base_sha": base_sha}

    monkeypatch.setattr(dispatch_module, "_create_worktree", prepare_worktree)
    return config_home, repo


def _completed_pointer(
    config_home: Path,
    repo: Path,
    run_id: str,
    *,
    role: str = "",
    role_on_node: bool = False,
) -> dict:
    """A completed run with no stored review, carrying the role it was dispatched with."""
    manifest = config_home / "manifests" / (run_id + ".md")
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        "node: " + run_id + "\nstatus: complete\ncommits: " + run_id + "\n",
        encoding="utf-8",
    )
    node = {"id": run_id, "plan": "fixture", "section": "s2"}
    record = {
        "run_id": run_id,
        "project": "sample",
        "repo": str(repo),
        "node": node,
        "backend": "alpha",
        "launch": "cli",
        "argv": ["codex"],
        "phase": "starting",
        "process_alive": False,
        "session": "session-orchestrating",
        "manifest_path": str(manifest),
    }
    if role:
        if role_on_node:
            node["role"] = role
        else:
            record["role"] = role
    crew._write_json(crew.pointer_path(run_id), record)
    return record


def _wait_for_stopped_producer() -> None:
    deadline = time.monotonic() + WATCHER_LOAD_BOUND_SECONDS
    while time.monotonic() < deadline:
        if not crew.watch_state("sample")["watcher_live"]:
            return
        time.sleep(0.05)
    pytest.fail("watch producer did not release its seat")


def test_a_completed_review_run_is_not_classified_as_scoring(
    isolated_project: tuple[Path, Path],
) -> None:
    """The positive half: the reviewer is not itself awaiting review."""
    config_home, repo = isolated_project
    record = _completed_pointer(config_home, repo, "r-prior-review", role="review")
    row = recovery.classify_pointer(record)
    assert row["classification"] == "promotable"
    assert row["classification"] != "scoring"


def test_a_completed_review_run_composes_no_review_of_itself(
    isolated_project: tuple[Path, Path],
) -> None:
    """The composed action names neither a review node nor the run it exempts."""
    config_home, repo = isolated_project
    record = _completed_pointer(config_home, repo, "r-prior-review", role="review")
    action = str(recovery.classify_pointer(record)["next_action"])
    assert recovery.REVIEW_NODE_PREFIX not in action
    assert "r-prior-review" not in action


def test_an_unreviewed_implement_run_still_classifies_as_scoring(
    isolated_project: tuple[Path, Path],
) -> None:
    """The negative half: the exemption is keyed on role, not on a missing review."""
    config_home, repo = isolated_project
    record = _completed_pointer(config_home, repo, "r-work", role="implement")
    row = recovery.classify_pointer(record)
    assert row["classification"] == "scoring"
    action = str(row["next_action"])
    assert "--role review" in action
    assert recovery.REVIEW_NODE_PREFIX + "r-work" in action


def test_the_role_is_read_from_either_spelling_the_pointer_may_car(
    isolated_project: tuple[Path, Path],
) -> None:
    """Dispatch writes the role on the root and on the node, so both are accepted."""
    config_home, repo = isolated_project
    on_node = _completed_pointer(
        config_home, repo, "r-node-role", role="review", role_on_node=True
    )
    assert recovery.classify_pointer(on_node)["classification"] != "scoring"
    implement_on_node = _completed_pointer(
        config_home, repo, "r-node-implement", role="implement", role_on_node=True
    )
    assert recovery.classify_pointer(implement_on_node)["classification"] == "scoring"


def test_dispatch_awaiting_reviews_reports_only_the_run_awaiting_review(
    isolated_project: tuple[Path, Path],
) -> None:
    """One implement run and one review run yield one report, not two."""
    config_home, repo = isolated_project
    _completed_pointer(config_home, repo, "r-work", role="implement")
    _completed_pointer(config_home, repo, "r-prior-review", role="review")
    calls: list[tuple] = []

    def launcher(*args, **kwargs):
        calls.append((args, kwargs))
        return os.getpid()

    try:
        with runs.follower_claim("sample", "session-orchestrating", delivery="stream"):
            result = recovery.dispatch_awaiting_reviews(
                project="sample", config=CONFIG, launcher=launcher
            )
        assert [report["run_id"] for report in result["reports"]] == ["r-work"]
        assert len(result["dispatched"]) == 1
        assert len(calls) == 1
        assert runs.read_pointer("r-work")["review_dispatch"]["status"] == "dispatched"
        assert "review_dispatch" not in runs.read_pointer("r-prior-review")
    finally:
        if crew.watch_state("sample")["watcher_live"]:
            recovery.unwatch("sample")
            _wait_for_stopped_producer()


def test_no_dispatch_ever_names_a_review_of_a_review(
    isolated_project: tuple[Path, Path],
) -> None:
    """The chain this node closes: no live run is a review of a review run."""
    config_home, repo = isolated_project
    _completed_pointer(config_home, repo, "r-work", role="implement")
    _completed_pointer(config_home, repo, "r-prior-review", role="review")
    try:
        with runs.follower_claim("sample", "session-orchestrating", delivery="stream"):
            recovery.dispatch_awaiting_reviews(
                project="sample", config=CONFIG, launcher=lambda *a, **k: os.getpid()
            )
        ids = [str((row.get("node") or {}).get("id") or "") for row in runs.list_live()]
        minted = [name for name in ids if name.startswith(recovery.REVIEW_NODE_PREFIX)]
        assert minted == [recovery.REVIEW_NODE_PREFIX + "r-work"]
        assert not [
            name
            for name in ids
            if name
            == recovery.REVIEW_NODE_PREFIX
            + recovery.REVIEW_NODE_PREFIX
            + "r-prior-review"
        ]
    finally:
        if crew.watch_state("sample")["watcher_live"]:
            recovery.unwatch("sample")
            _wait_for_stopped_producer()


def test_the_promotion_boundary_exempts_the_same_role_the_classifier_does(
    isolated_project: tuple[Path, Path],
) -> None:
    """The two exemptions must agree on one record, or one of them is wrong."""
    config_home, repo = isolated_project
    review_record = _completed_pointer(
        config_home, repo, "r-prior-review", role="review"
    )
    review_row = recovery.classify_pointer(review_record)
    assert review_row["classification"] != "scoring"
    assert (
        promotion._require_review_waiver(
            "r-prior-review",
            review_record,
            verdict="passed",
            classification=str(review_row["classification"]),
            review=None,
            review_action=str(review_row["next_action"]),
            waiver_reason="",
        )
        is None
    )

    implement_record = _completed_pointer(config_home, repo, "r-work", role="implement")
    implement_row = recovery.classify_pointer(implement_record)
    assert implement_row["classification"] == "scoring"
    with pytest.raises(CrewError):
        promotion._require_review_waiver(
            "r-work",
            implement_record,
            verdict="passed",
            classification=str(implement_row["classification"]),
            review=None,
            review_action=str(implement_row["next_action"]),
            waiver_reason="",
        )
