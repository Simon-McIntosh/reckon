"""The two revision-scoped history readers restate against the run store.

Velocity reads a project's ledger out of the tree at the "window's head", and
the review-cycle figure reads each checkout's ``docs/state/<project>/crew.json``
for dispatch times. Both are working-tree reads, so once the ledger leaves git
they answer nothing: velocity reports a project with no runs, and the figure
drops every dispatch time out of the cadence with nothing failing.

This module synthesises a repository under ``tmp_path`` whose runs enter the
tree one at a time and then leave it in a later commit, holds the same runs in
an isolated run store, and measures the two readers over that history:

* velocity's figures at the head whose tree carries no ledger equal the figures
  the same runs gave at the last revision that still held their files, over a
  population whose run count is nonzero and equals the ledger's;
* the review-cycle figure reports the same dispatch times it reported while the
  dispatch file was still in the tree;
* each store row carries the commit its own record file entered the tree at;
* an empty store is refused with the store's path named, rather than answered
  as zero runs or an empty mapping that would match the empty tree.

The declared mutation makes the empty store answer an empty population, and the
empty-store checks go red.
"""

from __future__ import annotations

import collections
import datetime as dt
import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest

from reckon import run_store, velocity

DAY = 86400
BASE = int(dt.datetime(2026, 9, 1, tzinfo=dt.UTC).timestamp())
PROJECT = "sample"
BRANCH = "main"
WORKTREE_ROOT = Path(__file__).resolve().parents[1]
FIGURE_SCRIPT = (
    WORKTREE_ROOT
    / "docs"
    / "figures"
    / "plan-review-cycles"
    / "measure_review_cycles.py"
)

_DISPATCH_IDENTITY = ("RECKON_RUN_ID", "RECKON_MANIFEST", "RECKON_ATTEMPT_STARTED_AT")

# The record fields the velocity figures are compared on: everything the capture
# projects from a record, minus the label naming which source answered, which is
# the one field the two arms are supposed to differ in.
FIGURE_FIELDS = (
    "run_id",
    "node",
    "plan",
    "role",
    "backend",
    "agent",
    "session_id",
    "dispatched_at",
    "completed_at",
    "completed_at_source",
    "worker_seconds",
    "wall_seconds",
    "gate",
    "commits",
    "base_sha",
    "attempt",
    "attempt_kind",
    "lineage",
    "promotion_commits",
    "resolved_commits",
    "unresolved_or_unreachable_commits",
)

STORE_SOURCES = frozenset({"run_store_record_revision", "run_store_row"})


def _iso(day: int, seconds: int = 0) -> str:
    return velocity.iso(BASE + day * DAY + seconds)


def _git(repo: Path, *arguments: str, env: dict | None = None) -> str:
    # The worker that runs this suite exports the identity of its own run, and a
    # git wrapper keyed on that identity refuses a mutating verb outside the
    # worker's worktree. Point the subprocess at the synthesised repository by
    # dropping the inherited identity.
    base = {**os.environ, **(env or {})}
    for name in _DISPATCH_IDENTITY:
        base.pop(name, None)
    result = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        check=True,
        capture_output=True,
        text=True,
        env=base,
    )
    return result.stdout.strip()


def _commit(repo: Path, message: str, day: int, changes: dict[str, str | None]) -> str:
    when = _iso(day)
    env = {
        **os.environ,
        "GIT_AUTHOR_DATE": when,
        "GIT_COMMITTER_DATE": when,
        "GIT_AUTHOR_NAME": "fixture",
        "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
        "GIT_COMMITTER_NAME": "fixture",
        "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
    }
    for path, content in changes.items():
        if content is None:
            _git(repo, "rm", "-q", "--ignore-unmatch", path, env=env)
            continue
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        _git(repo, "add", path, env=env)
    _git(repo, "commit", "-q", "--allow-empty", "-m", message, env=env)
    return _git(repo, "rev-parse", "HEAD", env=env)


def _record(run_id: str, index: int, day: int, source_sha: str, *, role: str) -> dict:
    return {
        "run_id": run_id,
        "node": f"node-{index}",
        "plan": "a-plan" if index < 2 else "b-plan",
        "role": role,
        "backend": "codex" if index % 2 else "clive",
        "agent": {
            "backend": "codex" if index % 2 else "clive",
            "model": "m1",
        },
        "session_id": f"session-{index}",
        "dispatched_at": _iso(day, 60),
        "completed_at": _iso(day, 660),
        "completed_at_source": "terminal_event",
        "worker_seconds": 600,
        "wall_seconds": 700,
        "gate": "passed",
        "commits": [source_sha[:12]],
        "base_sha": source_sha,
        "attempt": 1,
        "attempt_kind": "initial",
        "lineage": {},
    }


def _records(source_sha: str) -> tuple[tuple[int, dict], ...]:
    """Four runs and the day each lands, the last a review.

    The review exercises the figure's role filter: its dispatch time is not a
    build dispatch, so the script must leave it out of both arms.
    """
    return (
        (
            3,
            _record(
                "r-20260903T000000000000-node-0", 0, 3, source_sha, role="implement"
            ),
        ),
        (
            4,
            _record(
                "r-20260904T000000000000-node-1", 1, 4, source_sha, role="implement"
            ),
        ),
        (
            5,
            _record(
                "r-20260905T000000000000-node-2", 2, 5, source_sha, role="implement"
            ),
        ),
        (6, _record("r-20260906T000000000000-review", 3, 6, source_sha, role="review")),
    )


def _run_path(run_id: str) -> str:
    return f"docs/state/{PROJECT}/runs/{run_id}.json"


def _ledger_path() -> str:
    return f"docs/state/{PROJECT}/crew.json"


def _ledger_document(records: tuple[dict, ...]) -> str:
    return json.dumps(
        {"data": {"_version": 7, "members": [], "holds": [], "runs": list(records)}}
    )


class History:
    """One repository whose ledger enters the tree and then leaves it."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.repo = root / PROJECT
        self.store = root / "run_store.db"
        self.held_head = ""
        self.absent_head = ""
        self.additions: dict[str, str] = {}
        self.landings: tuple[tuple[int, dict], ...] = ()

    @property
    def records(self) -> tuple[dict, ...]:
        return tuple(record for _day, record in self.landings)

    def capture(self, end: str, *, run_store_db: Path | None = None) -> dict:
        return velocity.capture(
            {PROJECT: BRANCH},
            start=_iso(2),
            end=end,
            code_root=self.root,
            run_store_db=self.store if run_store_db is None else run_store_db,
        )

    def figures(self, snapshot: dict) -> list[dict]:
        return [
            {name: row[name] for name in FIGURE_FIELDS}
            for row in snapshot["projects"][0]["runs"]
        ]


def _build(root: Path) -> History:
    history = History(root)
    repo = history.repo
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", BRANCH)
    source = _commit(
        repo, "feat: a measured source change", 1, {"reckon/sample.py": "VALUE = 1\n"}
    )
    history.landings = _records(source)
    # Each run's own file enters the tree in its own commit, and the aggregate
    # carrying the same rows is rewritten beside it, so the commit that added a
    # run's file is a fact the store has to read rather than one shared sha.
    for position, (day, record) in enumerate(history.landings):
        # Only the first run lands under a promotion subject. The rest enter
        # under a plain record subject, so a reader cannot reconstruct them from
        # a promotion the graph names and the store is the only source holding
        # them once the files leave the tree.
        subject = (
            f"promote({record['run_id']}): land the run"
            if position == 0
            else f"docs: record({record['run_id']})"
        )
        added = _commit(
            repo,
            subject,
            day,
            {
                _run_path(record["run_id"]): json.dumps(record),
                _ledger_path(): _ledger_document(history.records[: position + 1]),
            },
        )
        history.additions[record["run_id"]] = added
    history.held_head = _git(repo, "rev-parse", "HEAD")
    return history


def _retire(history: History) -> None:
    """Remove the ledger and every run file from the tree, in one commit."""
    changes: dict[str, str | None] = {
        _run_path(record["run_id"]): None for record in history.records
    }
    changes[_ledger_path()] = None
    history.absent_head = _commit(
        history.repo, "docs: retire the committed ledger", 8, changes
    )


def _ledger_run_count(history: History, revision: str) -> int:
    document = json.loads(_git(history.repo, "show", f"{revision}:{_ledger_path()}"))
    return len(document["data"]["runs"])


def _figure_module():
    module_spec = importlib.util.spec_from_file_location(
        "measure_review_cycles_under_test", FIGURE_SCRIPT
    )
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    return module


@pytest.fixture()
def history(tmp_path, monkeypatch):
    """An isolated config home, an isolated store and a ledger that leaves git.

    The figure script sets a matplotlib style named ``data-ink`` that lives in
    the operator's own matplotlib config, so the import is given a config of its
    own holding one: the checks below measure the dispatch-time read, not the
    plotting the script's module scope also performs.
    """
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("RECKON_RUN_STORE", str(tmp_path / "run_store.db"))
    monkeypatch.delenv("RECKON_STATE_ROOT", raising=False)
    monkeypatch.delenv("RECKON_MOUNTS_PATH", raising=False)
    stylelib = tmp_path / "matplotlib" / "stylelib"
    stylelib.mkdir(parents=True)
    (stylelib / "data-ink.mplstyle").write_text("axes.facecolor: white\n")
    monkeypatch.setenv("MPLCONFIGDIR", str(tmp_path / "matplotlib"))
    built = _build(tmp_path)
    # The corpus import is how a project's history reaches the store, and it is
    # the pass that places each row at the commit its file entered the tree at.
    run_store.import_ledger(PROJECT, root=built.repo)
    return built


def test_velocity_restates_the_runs_from_the_store(history):
    held = history.figures(history.capture(_iso(7)))
    expected_ids = sorted(record["run_id"] for record in history.records)
    assert [row["run_id"] for row in held] == expected_ids, (
        "the fixture did not reach a nonzero measured population"
    )
    assert len(held) == _ledger_run_count(history, history.held_head) > 0

    _retire(history)
    assert not (history.repo / _ledger_path()).exists()
    assert not (history.repo / f"docs/state/{PROJECT}/runs").exists()
    assert not _git(
        history.repo,
        "ls-tree",
        "-r",
        "--name-only",
        history.absent_head,
        "--",
        f"docs/state/{PROJECT}",
    )

    snapshot = history.capture(_iso(9))
    project = snapshot["projects"][0]
    assert project["runs"], "velocity reported zero runs from a store holding four"
    assert history.figures(snapshot) == held
    assert {row["record_source"] for row in project["runs"]} <= STORE_SOURCES


def test_each_store_row_carries_the_commit_that_added_its_record_file(history):
    assert run_store.stored_run_revisions(PROJECT, path=history.store) == (
        history.additions
    )


def test_the_review_cycle_script_reads_its_dispatch_times_from_the_store(history):
    module = _figure_module()
    expected: dict[tuple[str, str], list] = collections.defaultdict(list)
    for record in history.records:
        if record["role"] == "review":
            continue
        moment = module.parse_ts(record["dispatched_at"]) or module.parse_ts(
            record["completed_at"]
        )
        expected[(PROJECT, record["plan"])].append(moment)
    for moments in expected.values():
        moments.sort()
    assert sum(len(value) for value in expected.values()) == 3

    _retire(history)
    assert not (history.repo / _ledger_path()).exists()

    assert module.load_dispatch_times({PROJECT}) == dict(expected)


@pytest.mark.parametrize("held", [True, False])
def test_an_empty_store_is_refused_rather_than_answered_as_zero(history, held):
    store = history.root / ("empty.db" if held else "absent.db")
    if held:
        with run_store.RunStore(store):
            pass

    _retire(history)
    with pytest.raises(RuntimeError) as refused:
        history.capture(_iso(9), run_store_db=store)
    assert str(store) in str(refused.value)
    assert PROJECT in str(refused.value)


def test_the_figure_refuses_an_empty_store_rather_than_reporting_no_times(
    history, monkeypatch
):
    module = _figure_module()
    store = history.root / "empty.db"
    with run_store.RunStore(store):
        pass
    monkeypatch.setenv("RECKON_RUN_STORE", str(store))

    _retire(history)
    with pytest.raises(RuntimeError) as refused:
        module.load_dispatch_times({PROJECT})
    assert str(store) in str(refused.value)


def test_a_promotion_row_names_no_revision_its_record_cannot_supply(history):
    """``promoted_revision`` is the run's work tip, not its record's commit.

    A promotion commits the run's record file in its own commit, and states that
    commit's parent as ``promoted_revision``; a row written from such a record
    therefore names a revision whose tree does not hold the file. The column is
    the commit the file entered the tree at, so the promotion path stamps
    nothing and the backfill reads the adding commit from history.
    """
    record = dict(history.records[2], promoted_revision="deadbeefcafe")
    store = history.root / "promotion.db"
    with run_store.RunStore(store) as opened:
        opened.append(PROJECT, record)
        assert opened.revisionless_rows(PROJECT) == {record["run_id"]}, (
            "a row the promotion wrote claimed a revision its record could not supply"
        )
    assert record["run_id"] not in (run_store.stored_run_revisions(PROJECT, path=store) or {})

    counts = run_store.backfill_revisions(PROJECT, root=history.repo, path=store)
    assert counts == {"revision_stamped": 1, "revision_unresolved": 0}
    revisions = run_store.stored_run_revisions(PROJECT, path=store)
    assert revisions is not None
    assert revisions[record["run_id"]] == history.additions[record["run_id"]]
    assert revisions[record["run_id"]] != record["promoted_revision"]


def test_a_store_row_counts_as_landed_from_the_record_the_branch_committed(history):
    """The landed rule reads a promoted revision only from a committed record.

    A store row is the store's copy of that same record, so a row re-served from
    it — or from the revision the record file was read at — carries the marker
    the rule wants; leaving those sources out under-counts landed nodes exactly
    on the path that survives the ledger leaving git.
    """
    run = {
        "run_id": history.records[0]["run_id"],
        "project": PROJECT,
        "promoted_revision": "deadbeefcafe",
        "coordinator": {"runtime_session_id": "session-coordinator"},
    }
    for source in sorted(STORE_SOURCES):
        cost = velocity.coordinator_cost(
            [dict(run, record_source=source)], transcript_root=None
        )
        assert cost["sessions"][0]["landed_nodes"] == 1, source
    cost = velocity.coordinator_cost(
        [dict(run, record_source="record_without_a_committed_copy")],
        transcript_root=None,
    )
    assert cost["sessions"][0]["landed_nodes"] == 0


def test_the_fallback_answers_the_project_of_a_row_whose_record_named_none(
    history,
):
    """A promotion the graph names, whose record never entered the tree.

    The store files a row under the project its caller passes to the append, so
    the fallback that reads such a row back must take the project from the runs
    column: a record a promotion writes names none of its own. The tree here
    still carries its documents, which is what leaves the promoted run missing
    from the records read and hands it to the fallback.
    """
    promoted = {
        "run_id": "r-20260905T000000000000-uncommitted",
        "member": "worker-a",
        "node": "task",
        "plan": "work",
        "role": "worker",
        "backend": "claude",
        "dispatched_at": _iso(5),
        "completed_at": _iso(5),
        "wall_seconds": 60,
        "gate": "passed",
    }
    with run_store.RunStore(history.store) as store:
        store.append(PROJECT, promoted)
    _commit(
        history.repo,
        f"promote({promoted['run_id']}): land the run",
        5,
        {"reckon/sample.py": "VALUE = 2\n"},
    )

    snapshot = history.capture(_iso(9))
    rows = {row["run_id"]: row for row in snapshot["projects"][0]["runs"]}
    assert rows, "the reading recovered no runs at all"
    recovered = rows[promoted["run_id"]]
    assert recovered["record_source"] == "sqlite_fallback_for_reachable_promotion"
    assert recovered["project"] == PROJECT
