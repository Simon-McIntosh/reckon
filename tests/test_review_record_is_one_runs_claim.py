"""One review record is one run's write claim.

A review's durable record is written under the crew configuration home, which
no repository contains. Two reviews of one subject at one head declare the same
record path, and the review store is where a lost write leaves nothing behind:
the loser of a collision on a repository path still leaves a commit or a dirty
tree, while the second writer of a review record overwrites the first with no
trace at all, and that record is the durable copy of the score.

The case declares one review record as the write path of a live review run and
dispatches a second review of the same subject under a fresh node id — the
shape a retry takes when a review dies seconds into its turn: a fresh node id
is the right call for manifest hygiene, and it is what puts two live pointers
against one file. The second dispatch must be refused by the scope conflict,
naming the first run and the shared record path.

The discriminating negative is the other half of the measure: a review of a
different subject holds a different record path and is admitted, so the case is
red both when out-of-repository paths are not compared at all and when every
out-of-repository path is refused wholesale.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from reckon import crew
from reckon.crew import review as review_module
from reckon.crew.runs import list_live, pointer_path

dispatch_module = importlib.import_module("reckon.crew.dispatch")

PROJECT = "review-record-fixture"
PLAN = "fixture"
SESSION = "review-record-session"
FIRST_RUN = "r-20260101T000000000000-review-of-one-run"
FIRST_NODE = "review-of-one-run"
SECOND_NODE = "review-of-one-run-retry"
OTHER_NODE = "review-of-another-run"
REVIEWED_RUN = "r-20251231T000000000000-reviewed-subject"
OTHER_REVIEWED_RUN = "r-20251231T000000000001-other-subject"
REVIEWED_HEAD = "0123456789abcdef0123456789abcdef01234567"
REAL_LIVE = Path.home() / ".config" / "reckon" / "crew" / "live"

CONFIG: dict[str, Any] = {
    "default_backend": "alpha",
    "backends": {
        "alpha": {
            "launch": "cli",
            "command": "codex",
            "model": "some-model",
            "effort": "high",
            "sandbox": "worktree-full",
            "session_reuse": False,
            "time_budget": "25m",
        }
    },
    "roles": {"review": {}},
    "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
}


def _git(repo: Path, *arguments: str) -> None:
    subprocess.run(["git", *arguments], cwd=repo, check=True, capture_output=True)


def _plan_document() -> str:
    head = "<!doctype html><html><head>"
    metas = (
        f'<meta name="docs-project" content="{PROJECT}">'
        '<meta name="reckon-type" content="plan">'
        '<meta name="plan-slug" content="fixture">'
        '<meta name="plan-title" content="fixture">'
        '<meta name="plan-status" content="active">'
        '<meta name="plan-impl" content="0">'
        '<meta name="plan-version" content="0">'
    )
    body = '<body><h2 id="s5">A review writes outside the repository</h2></body>'
    return f"{head}{metas}</head>{body}</html>"


def _fixture_pointers_in_the_real_home() -> list[str]:
    """Names of real-home pointers that this fixture's markers would name.

    The real pointer directory is read, never written: a case whose isolation
    holds has no pointer there mentioning this project or run.
    """
    found: list[str] = []
    for path in REAL_LIVE.glob("*.json"):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if PROJECT in text or FIRST_RUN in text:
            found.append(path.name)
    return found


@pytest.fixture()
def home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[Path, Path]]:
    """A temporary crew home and a repository that looks like a reckon mount."""
    assert _fixture_pointers_in_the_real_home() == []
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))

    repo = tmp_path / "repo"
    plans = repo / "docs" / "plans"
    plans.mkdir(parents=True)
    (plans / "fixture.html").write_text(_plan_document(), encoding="utf-8")
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "worker@example.invalid")
    _git(repo, "config", "user.name", "Worker")
    _git(repo, "add", "seed.txt", "docs")
    _git(repo, "commit", "-q", "-m", "chore: seed")
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repo / "docs")}), encoding="utf-8"
    )
    yield config_home, repo
    # An isolated read does not prove an isolated write: the real pointer
    # directory must be untouched by every case.
    assert _fixture_pointers_in_the_real_home() == []


def _record_paths(reviewed_run: str) -> list[str]:
    """The two durable paths a review of one run at one head writes."""
    return [
        str(review_module.review_path(PROJECT, reviewed_run)),
        str(
            review_module.review_path(
                PROJECT, reviewed_run, reviewed_head_sha=REVIEWED_HEAD
            )
        ),
    ]


def _publish_live_review(repo: Path, write_paths: list[str]) -> None:
    """A live review run holding the record paths, as its launch leaves it."""
    crew._write_json(
        pointer_path(FIRST_RUN),
        {
            "run_id": FIRST_RUN,
            "project": PROJECT,
            "repo": str(repo),
            "phase": "working",
            "pid": os.getpid(),
            "node": {"id": FIRST_NODE, "plan": PLAN, "write_paths": write_paths},
        },
    )


def _node(config_home: Path, node_id: str, write_paths: list[str]) -> crew.TaskNode:
    return crew.TaskNode(
        id=node_id,
        goal="attach an independent review to the recorded run",
        plan=PLAN,
        section="s5",
        role="review",
        spec_level="guided",
        done_when=(
            "the review store carries one record for the reviewed run: "
            "1 record, 0 added failures"
        ),
        write_paths=write_paths,
        time_budget="20m",
        manifest_path=str(config_home / "manifests" / (node_id + ".md")),
    )


def _dispatch(
    config_home: Path,
    repo: Path,
    node: crew.TaskNode,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, Any]:
    """A real review dispatch, with the worktree and the worker process stubbed.

    The refusal, when it fires, is raised before either stub is reached; the
    admission arm exists so the negative case can assert a dispatch that runs
    to a run id without spawning a worker.
    """
    worktree = tmp_path / "worktrees" / node.id

    def prepare_worktree(_repo: Path, _session: str, _node: str, base: str) -> dict:
        worktree.mkdir(parents=True, exist_ok=True)
        return {"path": str(worktree), "base": base, "base_sha": base}

    monkeypatch.setattr(dispatch_module, "_create_worktree", prepare_worktree)
    return crew.dispatch(
        node=_node(config_home, node.id, list(node.write_paths)),
        project=PROJECT,
        repo=repo,
        config=CONFIG,
        session=SESSION,
        launcher=lambda *_args, **_kwargs: os.getpid(),
        check_budget=False,
    )


def test_a_second_review_of_one_record_is_refused(
    home: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two live reviews of one subject may not share the one record path."""
    config_home, repo = home
    record_paths = _record_paths(REVIEWED_RUN)
    _publish_live_review(repo, record_paths)

    with pytest.raises(dispatch_module.ScopeConflict) as raised:
        _dispatch(
            config_home,
            repo,
            _node(config_home, SECOND_NODE, record_paths),
            tmp_path,
            monkeypatch,
        )

    refusal = raised.value
    assert refusal.run_id == FIRST_RUN
    assert refusal.node_id == FIRST_NODE
    assert refusal.claimed_path in record_paths
    assert refusal.claimed_path == _record_paths(REVIEWED_RUN)[0]
    assert FIRST_RUN in str(refusal)
    assert refusal.claimed_path in str(refusal)


def test_a_review_of_another_subject_is_admitted(
    home: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A review of a different subject holds a different record path."""
    config_home, repo = home
    _publish_live_review(repo, _record_paths(REVIEWED_RUN))

    record = _dispatch(
        config_home,
        repo,
        _node(config_home, OTHER_NODE, _record_paths(OTHER_REVIEWED_RUN)),
        tmp_path,
        monkeypatch,
    )

    run_id = str(record["run_id"])
    assert run_id.startswith("r-")
    assert {str(row["run_id"]) for row in list_live(project=PROJECT)} == {
        run_id,
        FIRST_RUN,
    }
    recorded = list(crew.read_pointer(run_id)["node"]["write_paths"])
    for path in _record_paths(OTHER_REVIEWED_RUN):
        assert path in recorded
    assert _record_paths(REVIEWED_RUN)[0] not in recorded
