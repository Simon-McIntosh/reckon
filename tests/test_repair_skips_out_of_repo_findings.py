"""A round no finding locates in the repository composes no repair.

The reflex's repair node is a brief against the repository under review: its
write scope is the paths the findings name, its gate the reviewed run's tests.
A round whose findings name only the fleet's own record — a manifest, a gate
log, a review-store path — therefore locates nothing in the repository to
answer, and dispatching a repair for it manufactures a lane with no work.

The defect this file guards is a decision read from the wrong place. The
composed write scope always carries the reviewed run's test paths, granted so
the repair can run that run's gate; a check asking whether *that* scope is empty
is never empty for any reviewed run holding a test path, so an all-record round
dispatches anyway. The decision must be read from the findings' own cited paths.

Both halves are asserted through a round's stored review driven through the
reflex with the dispatch stubbed: the all-record round records a decline-only
reason and launches nothing, and a round mixing a record path with a repository
source path still composes and dispatches, with only the repository path in
scope.

A second defect on the same path is a refusal read as an absence. The repair
inherits the reviewed run's suite command so its own review can reconcile its
added-failure count against the same suite; when the pointer names no command it
falls back to the project's standing ``review.suite`` declaration. That
declaration refusing by name — present but malformed — must reach the
coordinator as a refusal, not be caught and read as no standing suite.
"""

from __future__ import annotations

import importlib
import os
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from reckon import crew, flight
from reckon.crew import recovery, repair, resumption, runs
from reckon.crew import review as review_module
from reckon.crew.dispatch import WATCHER_LOAD_BOUND_SECONDS

# The reflex is gated by the watch admission, so these tests arm the producer
# the gate reads rather than accepting the suite-wide waiver, which would let
# every dispatch through and prove nothing about the refusal.
pytestmark = pytest.mark.arms_watch_producer

PROJECT = "sample"
RUN_ID = "r-work"
NODE_ID = "a-reviewed-node"


def _finding(path: str, line: str, text: str) -> dict[str, str]:
    return {"file": path, "line": line, "text": text}


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
def isolated_project(tmp_path: Path, monkeypatch) -> tuple[Path, Path, str]:
    """A project whose review store, ledger and repo all live under a temp root."""
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
    (scripts / "worktree_fleet.py").write_text(
        source.read_text(encoding="utf-8"), encoding="utf-8"
    )
    plans = repo / "docs" / "plans"
    plans.mkdir(parents=True, exist_ok=True)
    (plans / "fixture.html").write_text(
        '<meta name="docs-project" content="sample">'
        '<meta name="reckon-type" content="plan">'
        '<meta name="plan-slug" content="fixture">'
        '<h2 id="s2">A finding-bearing review dispatches its repair</h2>',
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
    return config_home, repo, base_sha


def _completed_pointer(
    config_home: Path | None = None,
    repo: Path | None = None,
    *,
    role: str = "implement",
    node_id: str = NODE_ID,
    **extra: object,
) -> dict:
    """The reviewed run: a completed implement run whose manifest reports completion.

    The node declares no write path, so the completed run holds no claim the
    repair's scope could collide with. The reviewed run's own fence is the
    composer's concern, exercised where it is composed; here it would only mask
    whether the reflex dispatched, by turning the observation into a scope
    refusal that has nothing to do with the reflex.
    """
    config_home = config_home if config_home is not None else _config_home()
    repo = repo if repo is not None else _config_home().parent / "repo"
    manifest = config_home / "manifests" / (RUN_ID + ".md")
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        "node: " + RUN_ID + "\nstatus: complete\ncommits: " + RUN_ID + "\n",
        encoding="utf-8",
    )
    record = {
        "run_id": RUN_ID,
        "project": PROJECT,
        "repo": str(repo),
        "role": role,
        "node": {"id": node_id, "plan": "fixture", "section": "s2"},
        "backend": "alpha",
        "launch": "cli",
        "argv": ["codex"],
        "phase": "starting",
        "process_alive": False,
        "session": "session-orchestrating",
        "manifest_path": str(manifest),
    }
    record.update(extra)
    crew._write_json(crew.pointer_path(RUN_ID), record)
    return record


def _config_home() -> Path:
    return Path(os.environ["RECKON_HOME"])


def _store_review(head_sha: str, findings: list[dict[str, str]]) -> None:
    """Write the reviewed run's review record into the isolated store root."""
    record = {
        "project": PROJECT,
        "reviewed_run_id": RUN_ID,
        "reviewed_base_sha": head_sha,
        "reviewed_head_sha": head_sha,
        "status": "parsed",
        "scores": dict.fromkeys(review_module.REVIEW_DIMENSIONS, 15),
        "total": 75,
        "findings": findings,
    }
    review_module.store_review(record)


def _wait_for_stopped_producer() -> None:
    deadline = time.monotonic() + WATCHER_LOAD_BOUND_SECONDS
    while time.monotonic() < deadline:
        if not crew.watch_state(PROJECT)["watcher_live"]:
            return
        time.sleep(0.05)
    pytest.fail("watch producer did not release its seat")


@contextmanager
def _armed_fleet():
    """Hold the follower claim the dispatch is gated on, then release the watch."""
    with runs.follower_claim(PROJECT, "session-orchestrating", delivery="stream"):
        try:
            yield
        finally:
            if crew.watch_state(PROJECT)["watcher_live"]:
                recovery.unwatch(PROJECT)
                _wait_for_stopped_producer()


@contextmanager
def _stubbed_dispatch(monkeypatch, *, repair_run_id: str = "r-repair-stub"):
    """Replace the dispatch with a recorder so the launch call can be read.

    The reflex dispatches through ``reckon.crew.dispatch.dispatch``, so a stub
    on that attribute observes the exact call the reflex composes — its base and
    the node whose write scope it carries — without a worktree being cut.
    """
    dispatch_module = importlib.import_module("reckon.crew.dispatch")
    calls: list[dict] = []

    def fake_dispatch(**kwargs):
        calls.append(kwargs)
        return {"run_id": repair_run_id}

    monkeypatch.setattr(dispatch_module, "dispatch", fake_dispatch)
    yield calls


def _repair_calls(calls: list[dict]) -> list[dict]:
    """The recorded calls whose node is a composed repair, in dispatch order."""
    return [
        call
        for call in calls
        if str(getattr(call["node"], "id", "")).startswith(repair.REPAIR_NODE_PREFIX)
    ]


def test_a_round_with_no_repository_finding_dispatches_nothing(
    isolated_project: tuple[Path, Path, str], monkeypatch
) -> None:
    """Every finding naming the fleet's own record leaves the round decline-only.

    The reviewed run holds a test path in its fence, so the composed write scope
    is non-empty however the findings read; a decision taken there would dispatch
    a repair with nothing in the repository to answer. The three findings name a
    manifest, a gate log and a review-store path — all outside the repository —
    so the round must record a decline-only reason and launch nothing.
    """
    config_home, repo, head_sha = isolated_project
    run_dir = Path(runs.run_dir(RUN_ID))
    _completed_pointer(
        config_home,
        repo,
        node={
            "id": NODE_ID,
            "plan": "fixture",
            "section": "s2",
            "write_paths": ["tests/test_reviewed_run.py"],
        },
    )
    _store_review(
        head_sha,
        [
            _finding(
                str(run_dir / "manifest.md"),
                "1",
                "the manifest reports the wrong count",
            ),
            _finding(
                str(run_dir / "gate.log"),
                "6",
                "the gate log ends without a verdict",
            ),
            _finding(
                str(review_module.review_path(PROJECT, RUN_ID)),
                "2",
                "the review overstates its evidence",
            ),
        ],
    )
    with _stubbed_dispatch(monkeypatch) as calls, _armed_fleet():
        report = resumption.sweep(PROJECT, config=CONFIG)

    assert _repair_calls(calls) == []
    assert report["reviews"]["repaired"] == []
    recorded = runs.read_pointer(RUN_ID)["repair_dispatch"]
    assert recorded["status"] == "decline-only"
    assert "no finding cites a repository path" in recorded["reason"]


def test_a_mixed_round_dispatches_with_only_the_repository_path(
    isolated_project: tuple[Path, Path, str], monkeypatch
) -> None:
    """One repository finding still dispatches, and the record path is not granted."""
    config_home, repo, head_sha = isolated_project
    run_dir = Path(runs.run_dir(RUN_ID))
    _completed_pointer(config_home, repo)
    _store_review(
        head_sha,
        [
            _finding(
                str(run_dir / "manifest.md"),
                "1",
                "the manifest reports the wrong count",
            ),
            _finding(
                "reckon/crew/recovery.py",
                "1700",
                "the empty-scope check reads the composed write scope",
            ),
        ],
    )
    with _stubbed_dispatch(monkeypatch) as calls, _armed_fleet():
        resumption.sweep(PROJECT, config=CONFIG)

    repairs = _repair_calls(calls)
    assert len(repairs) == 1
    scope = list(repairs[0]["node"].write_paths)
    landing = [
        path for path in scope if path.startswith(("docs/evidence/", "docs/figures/"))
    ]
    finding_scope = [path for path in scope if path not in landing]
    assert finding_scope == ["reckon/crew/recovery.py"]
    assert not any(Path(path).is_absolute() or path.startswith("~") for path in scope)


def test_the_repair_carries_the_reviewed_runs_suite_command(
    isolated_project: tuple[Path, Path, str], monkeypatch
) -> None:
    """The composed repair inherits the reviewed run's recorded suite command.

    The repair's own review derives its added-failure count from the pair of
    suite observations its manifest records, and it can only reconcile that pair
    against the reviewed run when both were measured with the same command. The
    dispatch therefore hands the reviewed run's own suite command over, so the
    repair's live pointer records it rather than a null the review cannot use.
    """
    config_home, repo, head_sha = isolated_project
    _completed_pointer(config_home, repo, suite_command="uv run pytest -q")
    _store_review(
        head_sha,
        [
            _finding(
                "reckon/crew/recovery.py",
                "1748",
                "the repair drops the reviewed run's suite command",
            )
        ],
    )
    with _stubbed_dispatch(monkeypatch) as calls, _armed_fleet():
        resumption.sweep(PROJECT, config=CONFIG)

    repairs = _repair_calls(calls)
    assert len(repairs) == 1
    assert repairs[0]["config"]["gates"]["suite_command"] == "uv run pytest -q"


def test_the_repair_falls_back_to_the_projects_standing_suite(
    isolated_project: tuple[Path, Path, str], monkeypatch
) -> None:
    """A reviewed pointer naming no suite inherits the standing declaration.

    A run recorded before the project declared a standing suite carries no
    ``suite_command`` of its own, so the repair falls back to the ``review.suite``
    command the project declares now, joining its argument vector into the same
    string a recorded suite command carries.
    """
    config_home, repo, head_sha = isolated_project
    _completed_pointer(config_home, repo)
    _store_review(
        head_sha,
        [
            _finding(
                "reckon/crew/recovery.py",
                "1748",
                "the repair drops the reviewed run's suite command",
            )
        ],
    )
    standing = {
        **CONFIG,
        "review": {
            "suite": {"command": ["uv", "run", "pytest", "-q"], "budget": "10m"}
        },
    }
    with _stubbed_dispatch(monkeypatch) as calls, _armed_fleet():
        resumption.sweep(PROJECT, config=standing)

    repairs = _repair_calls(calls)
    assert len(repairs) == 1
    assert repairs[0]["config"]["gates"]["suite_command"] == "uv run pytest -q"


def test_a_malformed_standing_suite_is_refused_by_name() -> None:
    """A present-but-malformed standing suite is not read as no standing suite.

    ``flight.review_suite`` returns ``None`` for an undeclared suite and refuses
    a declaration that is present but malformed by raising ``FlightConfigError``.
    The two are different states, and reading the second as the first launches
    the repair with no inherited suite and no signal that anything was declared.
    The refusal must propagate from the helper, so the caller can report it.
    """
    malformed = {"review": {"suite": {"command": "uv run pytest -q", "budget": "10m"}}}
    with pytest.raises(flight.FlightConfigError):
        recovery._reviewed_run_suite_command({"suite_command": ""}, malformed)


def test_an_undeclared_standing_suite_is_no_command() -> None:
    """An absent standing suite stays an absence, not a refusal.

    The companion to the malformed case: a project declaring no ``review.suite``
    at all inherits no command, exactly as a fresh run with no standing suite
    carries none. Reading this absence as a refusal would refuse every repair in
    a project that declares no standing suite.
    """
    assert recovery._reviewed_run_suite_command({"suite_command": ""}, {}) == ""


def test_the_reflex_refuses_a_malformed_standing_suite_without_dispatching(
    isolated_project: tuple[Path, Path, str], monkeypatch
) -> None:
    """A malformed standing suite is recorded as a refusal, not launched silently.

    Driven through the reflex: the reviewed run names no suite command of its
    own, so the repair falls back to the standing declaration, which is present
    but malformed. The reflex must record the refusal against the reviewed run
    and dispatch nothing — the swallow this repair answers converted that
    refusal into a repair launched with no inherited suite.
    """
    config_home, repo, head_sha = isolated_project
    _completed_pointer(config_home, repo)
    _store_review(
        head_sha,
        [
            _finding(
                "reckon/crew/recovery.py",
                "1748",
                "the repair drops the reviewed run's suite command",
            )
        ],
    )
    malformed = {
        **CONFIG,
        "review": {"suite": {"command": "uv run pytest -q", "budget": "10m"}},
    }
    with _stubbed_dispatch(monkeypatch) as calls, _armed_fleet():
        resumption.sweep(PROJECT, config=malformed)

    assert _repair_calls(calls) == []
    recorded = runs.read_pointer(RUN_ID)["repair_dispatch"]
    assert recorded["status"] == "refused"
    assert "malformed" in recorded["reason"]
