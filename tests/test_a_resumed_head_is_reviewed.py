"""A review round of one run carries its own identity, keyed to the head it read.

The cause this file pins down is recorded in the plan §2's report as one of the
two misses the reflex failed to launch: a re-review of a resumed run reused the
first round's node id, so its worktree path collided with the first round's and
the launch failed. The fix gives every round a node id that carries the head it
reviews — ``<prefix><source>-<head[:12]>`` by ``repair_node_id``'s shared rule —
so a second round cannot collide with the first, and both id readers strip the
suffix so a later round still resolves to the first round's own run.

Every case is driven end to end against a real git history: the reviewed run's
pointer, its stored review and the reflex's own dispatch are the real ones, and
the worktree the dispatch asks for is captured at creation so a round's identity
and its path are read from the call that used them.
"""

from __future__ import annotations

import importlib
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reckon.crew import recovery, runs
from reckon.crew import review as review_module
from reckon.crew.repair import round_node_id

dispatch_module = importlib.import_module("reckon.crew.dispatch")
dispatch_sessions = importlib.import_module("reckon.crew.dispatch_sessions")
recovery_review_delivery = importlib.import_module(
    "reckon.crew.recovery_review_delivery"
)

PROJECT = "resumed-head-review-fixture"
SESSION = "coordinator-fixture"
OBSERVED_AT = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)

LOCAL_BACKEND = "clive"
OTHER_BACKEND = "delta"

# The run-review node-id prefix, spelled here rather than imported so the test
# pins the identity grammar rather than whatever the module happens to export.
REVIEW_NODE_PREFIX = "review-of-"


def _backend(command: str) -> dict:
    return {
        "launch": "cli",
        "command": command,
        "model": "some-model",
        "effort": "high",
        "sandbox": "worktree-full",
        "session_reuse": True,
        "time_budget": "25m",
    }


CONFIG = {
    "default_backend": LOCAL_BACKEND,
    "local_backend": LOCAL_BACKEND,
    "backends": {
        LOCAL_BACKEND: _backend("codex"),
        OTHER_BACKEND: _backend("claude"),
    },
    "roles": {"implement": {}, "review": {}},
    "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
}


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.strip()


def _commit(repository: Path, paths: dict[str, str], message: str) -> str:
    for name, body in paths.items():
        target = repository / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    subprocess.run(
        ["git", "add", *paths], cwd=repository, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", message],
        cwd=repository,
        check=True,
        capture_output=True,
    )
    return _git(repository, "rev-parse", "HEAD")


@pytest.fixture()
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    """A repository, an isolated crew home and a reviewed implement run."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    repo = tmp_path / "repo"
    repo.mkdir()
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
    ):
        _git(repo, *arguments)
    _commit(repo, {"seed.txt": "seed\n"}, "chore: seed")
    reviewed = _commit(
        repo,
        {
            "pkg/mod.py": "VALUE = 1\n",
            "docs/plans/fixture-plan.html": (
                '<meta name="docs-project" content="' + PROJECT + '">'
                '<meta name="reckon-type" content="plan">'
                '<meta name="plan-slug" content="fixture-plan">'
                '<h2 id="s2">A re-review round carries its own identity</h2>'
            ),
        },
        "feat: the reviewed change",
    )
    (config_home / "mounts.json").write_text(
        '{"' + PROJECT + '": "' + str(repo / "docs") + '"}'
    )
    monkeypatch.setattr(
        review_module,
        "review_store_root",
        lambda base_dir=None: config_home / "reviews",
    )
    return {
        "tmp": tmp_path,
        "config_home": config_home,
        "repo": repo,
        "reviewed": reviewed,
    }


def _write_run(
    world: dict,
    *,
    run_id: str,
    head: str,
    node_id: str | None = None,
    extra: dict | None = None,
) -> dict:
    """One complete, unpromoted implement run whose worktree is the repo."""
    repo = world["repo"]
    manifest = world["config_home"] / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        f"node: {run_id}\nstatus: complete\ncommits: [{head}]\n", encoding="utf-8"
    )
    pointer = {
        "run_id": run_id,
        "project": PROJECT,
        "session": SESSION,
        "repo": str(repo),
        "worktree": str(repo),
        "process_alive": False,
        "launch": "in-harness",
        "role": "implement",
        "manifest_path": str(manifest),
        "node": {
            "id": node_id or run_id,
            "plan": "fixture-plan",
            "write_paths": ["pkg/mod.py"],
        },
    }
    pointer.update(extra or {})
    runs._write_json(runs.pointer_path(run_id), pointer)
    return pointer


def _store_review(run_id: str, *, base: str, head: str) -> None:
    """Store a complete, clean review of ``run_id`` at the revision it read."""
    emitted = "\n".join(
        f"SCORE {dimension}: 20" for dimension in review_module.REVIEW_DIMENSIONS
    )
    stored = review_module.parse_review(emitted)
    stored.update(
        {
            "project": PROJECT,
            "reviewed_run_id": run_id,
            "review_run_id": f"review-of-{run_id}",
            "reviewed_base_sha": base,
            "reviewed_head_sha": head,
        }
    )
    review_module.store_review(stored)


def _drive_dispatch(
    world: dict,
    record: dict,
    *,
    monkeypatch: pytest.MonkeyPatch,
    captured: dict,
) -> dict:
    """Run the reflex for one run, capturing the worktree it asked for.

    The worktree creator is replaced so the node id and path a round composes
    are read from the call that used them, and the path is derived from the
    worker name exactly as the fleet script derives it, so two rounds at
    different heads land in two directories rather than one.
    """
    repo = world["repo"]
    worktrees = world["tmp"] / "worktrees"

    def prepare(_repo, session, worker, base):
        path = worktrees / str(session) / str(worker)
        path.mkdir(parents=True, exist_ok=True)
        captured["worker"] = worker
        captured["path"] = str(path)
        return {
            "path": str(path),
            "base": base,
            "base_sha": _git(repo, "rev-parse", "HEAD"),
        }

    monkeypatch.setattr(dispatch_module, "_create_worktree", prepare)
    from reckon.crew.recovery_review_dispatch import dispatch_review_for_run

    try:
        with runs.follower_claim(PROJECT, SESSION, delivery="stream"):
            return dispatch_review_for_run(
                record, config=CONFIG, launcher=lambda *a, **k: os.getpid()
            )
    finally:
        from reckon import crew

        if crew.watch_state(PROJECT)["watcher_live"]:
            recovery.unwatch(PROJECT)


def test_a_runtime_source_resume_launches_a_re_review_whose_id_and_path_differ(
    world, monkeypatch: pytest.MonkeyPatch
):
    """The §2 cause: a re-review reused the first round's id and collided.

    A run reviewed at head A is resumed onto head B by a runtime-source commit.
    The reflex must launch exactly one re-review keyed to B, under an id and a
    worktree path that differ from the first round's — the collision this
    section repairs.
    """
    repo = world["repo"]
    reviewed = world["reviewed"]
    run_id = "r-runtime-source-resume"
    _write_run(world, run_id=run_id, head=reviewed)
    _store_review(run_id, base=reviewed, head=reviewed)
    first_round_id = round_node_id(REVIEW_NODE_PREFIX, run_id, reviewed)
    first_round_path = world["tmp"] / "worktrees" / SESSION / first_round_id

    new_head = _commit(repo, {"pkg/mod.py": "VERSION = 2\n"}, "fix: a source move")
    record = runs.read_pointer(run_id)

    captured: dict = {}
    report = _drive_dispatch(world, record, monkeypatch=monkeypatch, captured=captured)

    assert report["dispatched"] is True, report.get("reason")
    assert report["review_tier"] == "light"
    assert report["scope"] == ["pkg/mod.py"]
    # The launched round's id carries head B and differs from the first's, and
    # its worktree path follows the id, so the two rounds never collide.
    assert captured["worker"] == round_node_id(REVIEW_NODE_PREFIX, run_id, new_head)
    assert captured["worker"] != first_round_id
    assert captured["path"] != str(first_round_path)
    assert captured["worker"].endswith(new_head[:12])


def test_a_data_only_resume_carries_the_stored_review_forward_and_launches_none(
    world, monkeypatch: pytest.MonkeyPatch
):
    """A move that changes only data is carried, so nothing launches.

    The stored review stands at the new head and the reflex composes no
    dispatch: no worktree is created and no review run is minted.
    """
    repo = world["repo"]
    reviewed = world["reviewed"]
    run_id = "r-data-only-resume"
    _store_review(run_id, base=reviewed, head=reviewed)
    _write_run(world, run_id=run_id, head=reviewed)

    new_head = _commit(repo, {"data/record.txt": "payload\n"}, "chore(data): record")
    record = runs.read_pointer(run_id)

    captured: dict = {}
    report = _drive_dispatch(world, record, monkeypatch=monkeypatch, captured=captured)

    assert report["dispatched"] is False
    assert report.get("carried") is True
    assert captured == {}  # no worktree was ever asked for
    carried = review_module.read_review(PROJECT, run_id, reviewed_head_sha=new_head)
    assert carried is not None
    assert recovery.same_revision(carried["reviewed_head_sha"], new_head)


def test_a_run_whose_stored_review_is_keyed_to_its_head_gets_no_launch(
    world, monkeypatch: pytest.MonkeyPatch
):
    """A run whose stored review already describes its head is not re-reviewed.

    The head did not move, so no round is owed: the reflex reads the stored
    record as evidence for the current head and launches nothing.
    """
    reviewed = world["reviewed"]
    run_id = "r-reviewed-at-its-head"
    _write_run(world, run_id=run_id, head=reviewed)
    _store_review(run_id, base=reviewed, head=reviewed)
    record = runs.read_pointer(run_id)

    captured: dict = {}
    report = _drive_dispatch(world, record, monkeypatch=monkeypatch, captured=captured)

    assert report["dispatched"] is False
    assert report["run_id"] == run_id
    assert "not awaiting review" in report["reason"]
    assert captured == {}


def test_a_third_round_gets_a_distinct_id_and_both_readers_resolve_every_round(
    world,
):
    """Every round of one run is a distinct id, and both readers see the run.

    Three rounds of one run stand at three heads. Their ids and paths are all
    distinct, and each id — the third included — resolves to the run reviewed,
    through both the session task identity and the delivery reader, which strip
    the head suffix the id now carries.
    """
    repo = world["repo"]
    run_id = "r-three-rounds"
    source_node = "implementer-node"  # a node id that is not the run id
    head_a = world["reviewed"]
    head_b = _commit(repo, {"pkg/mod.py": "VALUE = 2\n"}, "feat: round two source")
    head_c = _commit(repo, {"pkg/mod.py": "VALUE = 3\n"}, "feat: round three source")
    _write_run(world, run_id=run_id, head=head_c, node_id=source_node)

    round_ids = [
        round_node_id(REVIEW_NODE_PREFIX, source_node, head)
        for head in (head_a, head_b, head_c)
    ]
    assert len(set(round_ids)) == 3
    paths = {world["tmp"] / "worktrees" / SESSION / node_id for node_id in round_ids}
    assert len(paths) == 3

    books: list[tuple[str, str, dict]] = []
    records: list[dict] = [runs.read_pointer(run_id)]
    for index, node_id in enumerate(round_ids):
        review_run_id = f"r-review-round-{index}"
        pointer = {
            "run_id": review_run_id,
            "project": PROJECT,
            "role": "review",
            "node": {"id": node_id},
        }
        runs._write_json(runs.pointer_path(review_run_id), pointer)
        records.append(pointer)
        books.append((node_id, review_run_id, pointer))

    for node_id, review_run_id, pointer in books:
        assert node_id.startswith(REVIEW_NODE_PREFIX)
        assert pointer["run_id"] == review_run_id
        # The delivered-review reader resolves the run through the node id.
        assert recovery_review_delivery._resolved_reviewed_run_id(pointer, PROJECT) == (
            run_id
        )
        # The session task identity resolves it the same way, so a later round
        # continues the reviewed run's task rather than starting a fresh one.
        assert dispatch_sessions._task_identity(pointer, PROJECT, records) == (
            "review",
            PROJECT,
            run_id,
        )
