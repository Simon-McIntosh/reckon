"""A finding-bearing review round dispatches exactly one repair through the reflex.

The composer (:mod:`reckon.crew.repair`) turns a stored review record into a
node; this test drives that node *through the reflex* — a real periodic sweep
over a live pointer whose stored review carries findings — and asserts the
reflex dispatches it exactly once. Both failure modes here look like a quiet
fleet rather than an error: a sweep that dispatches nothing is indistinguishable
from a round with no findings, and a sweep that re-fires manufactures a second
repair for a round that already has one. So the negative halves are asserted as
carefully as the positive one.

The reflex's guards are the reason it can be re-landed at all, and each was
measured against a live failure: a run whose own worker is live or was resumed
on the same finding is left to that worker; a run promoted in the window
between composition and launch is skipped, re-read at the launch; a review of a
review, an investigate or test role, and a repair's own findings each open no
repair; and a finding that names only a run record or an evidence document
grants no scope and dispatches nothing.

The finding ids the brief must name are re-derived here from each finding's own
file, line and text, so the assertion holds against an expectation the module
cannot satisfy by returning a constant.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from reckon import crew, ledger
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


# The three findings the stored review carries. They name repository source and
# test paths, so the only thing that can refuse the repair dispatch is the
# reflex's own logic rather than a scope collision with a peer.
FINDINGS = [
    _finding("reckon/crew/thing.py", "10", "off-by-one in the loop"),
    _finding("tests/test_thing.py", "3", "the test asserts a stale value"),
    _finding("reckon/crew/new_module.py", "5", "a branch no test reaches"),
]


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


def _expected_id(finding: dict[str, str]) -> str:
    """The id the documented rule yields for one finding, re-derived here."""
    material = "\x00".join(
        (finding["file"].strip(), finding["line"].strip(), finding["text"].strip())
    )
    return "f" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:10]


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


def _write_worker_record(
    *, pid: int, launched_at: str = "2026-09-29T00:00:00+00:00"
) -> Path:
    """Write the run's own worker record naming a pid, as a supervisor would."""
    directory = Path(runs.run_dir(RUN_ID))
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / recovery.WORKER_RECORD_NAME
    path.write_text(
        json.dumps({"pid": pid, "launched_at": launched_at}), encoding="utf-8"
    )
    return path


def _write_resume_stream() -> Path:
    """Write a resumed turn's stream holding an assistant record."""
    directory = Path(runs.run_dir(RUN_ID))
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "resume-1.jsonl"
    path.write_text(
        json.dumps({"type": "assistant", "message": {"content": "working"}}) + "\n",
        encoding="utf-8",
    )
    return path


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


def _launcher_recording(calls: list[dict]):
    """A launcher that records every call and reports success without spawning."""

    def launcher(plan, *, log_path, stderr_path, prompt_path):
        calls.append(
            {
                "argv": list(getattr(plan, "argv", ())),
                "log_path": str(log_path),
                "prompt_path": str(prompt_path),
            }
        )
        return os.getpid()

    return launcher


def _sweep(calls: list[dict]) -> dict:
    with _armed_fleet():
        return resumption.sweep(
            PROJECT, config=CONFIG, launcher=_launcher_recording(calls)
        )


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


def test_a_three_finding_review_dispatches_exactly_one_repair(
    isolated_project: tuple[Path, Path, str],
) -> None:
    """The positive half: the reflex composes and dispatches the repair itself."""
    config_home, repo, head_sha = isolated_project
    _completed_pointer(config_home, repo)
    _store_review(head_sha, FINDINGS)
    calls: list[dict] = []
    report = _sweep(calls)

    repaired = report["reviews"]["repaired"]
    assert len(repaired) == 1
    assert len(calls) == 1

    pointer = runs.read_pointer(repaired[0])
    node = pointer["node"]
    goal = str(node.get("goal") or "")
    scope = list(node.get("write_paths") or [])
    assert str(node.get("id") or "").startswith(repair.REPAIR_NODE_PREFIX)
    # The brief names every finding by its content-derived id, and the write
    # scope the repair was dispatched with names every path the findings name:
    # one node, all three findings, no coordinator command in between.
    for finding in FINDINGS:
        assert _expected_id(finding) in goal
        assert finding["file"] in scope

    # The scope is narrowed to the findings' own repository paths: no run
    # directory and no review-store path is granted, and no absolute path
    # appears. The dispatch adds the node's own evidence fragment and figure,
    # which are its landing record rather than a finding's file.
    landing = [
        path for path in scope if path.startswith(("docs/evidence/", "docs/figures/"))
    ]
    finding_scope = [path for path in scope if path not in landing]
    assert set(finding_scope) == {finding["file"] for finding in FINDINGS}
    assert not any(Path(path).is_absolute() or path.startswith("~") for path in scope)

    recorded = runs.read_pointer(RUN_ID)["repair_dispatch"]
    assert recorded["status"] == "dispatched"
    assert recorded["run_id"] == repaired[0]


def test_the_repair_worktree_is_cut_from_the_reviewed_head(
    isolated_project: tuple[Path, Path, str], monkeypatch
) -> None:
    """An unpromoted reviewed run's repair is based on the head the review read."""
    config_home, repo, head_sha = isolated_project
    _completed_pointer(config_home, repo)
    _store_review(head_sha, FINDINGS)
    with _stubbed_dispatch(monkeypatch) as calls, _armed_fleet():
        resumption.sweep(PROJECT, config=CONFIG)

    repairs = _repair_calls(calls)
    assert len(repairs) == 1
    # The base handed to the dispatch is the reviewed head, so the reviewed head
    # is an ancestor of the repair's tree — not the branch tip, which the review
    # never read.
    assert repairs[0]["base"] == head_sha


def test_the_repair_keeps_the_reviewed_runs_test_paths(
    isolated_project: tuple[Path, Path, str], monkeypatch
) -> None:
    """A finding citing only source still grants the reviewed run's test paths.

    The repair's gate is the reviewed run's own tests. A fence holding two
    source paths and a test path, with the finding naming only one source, must
    carry the test path into the repair's scope; the source path no finding
    named must not be carried, so a composer that granted the whole fence would
    fail this test rather than pass it.
    """
    config_home, repo, head_sha = isolated_project
    reviewed_fence = [
        "reckon/crew/thing.py",
        "reckon/crew/new_module.py",
        "tests/test_reviewed_run.py",
    ]
    _completed_pointer(
        config_home,
        repo,
        node={
            "id": NODE_ID,
            "plan": "fixture",
            "section": "s2",
            "write_paths": list(reviewed_fence),
        },
    )
    _store_review(
        head_sha, [_finding("reckon/crew/thing.py", "10", "off-by-one in the loop")]
    )
    with _stubbed_dispatch(monkeypatch) as calls, _armed_fleet():
        resumption.sweep(PROJECT, config=CONFIG)

    repairs = _repair_calls(calls)
    assert len(repairs) == 1
    scope = list(repairs[0]["node"].write_paths)
    assert "reckon/crew/thing.py" in scope
    assert "tests/test_reviewed_run.py" in scope
    # The reviewed fence's uncited source path is not carried, so the repair is
    # not granted a file no finding named.
    assert "reckon/crew/new_module.py" not in scope


def test_a_second_sweep_over_a_live_round_dispatches_nothing(
    isolated_project: tuple[Path, Path, str],
) -> None:
    """Idempotence while the repair is live: the standing round is found."""
    config_home, repo, head_sha = isolated_project
    _completed_pointer(config_home, repo)
    _store_review(head_sha, FINDINGS)
    calls: list[dict] = []
    launcher = _launcher_recording(calls)
    with _armed_fleet():
        first = resumption.sweep(PROJECT, config=CONFIG, launcher=launcher)
        second = resumption.sweep(PROJECT, config=CONFIG, launcher=launcher)
    assert len(first["reviews"]["repaired"]) == 1
    assert second["reviews"]["repaired"] == []
    assert len(calls) == 1


def test_a_promoted_repair_leaves_the_round_dispatching_nothing(
    isolated_project: tuple[Path, Path, str],
) -> None:
    """Idempotence after promotion: the ledger, not the live pointer, is the record."""
    config_home, repo, head_sha = isolated_project
    _completed_pointer(config_home, repo)
    _store_review(head_sha, FINDINGS)
    calls: list[dict] = []
    launcher = _launcher_recording(calls)
    with _armed_fleet():
        first = resumption.sweep(PROJECT, config=CONFIG, launcher=launcher)
        repair_run_id = first["reviews"]["repaired"][0]
        # Promote the repair run: it lands a ledger row and its live pointer is
        # reconciled away. The round is now satisfied, so the freed pointer must
        # not read as an unattempted round.
        run_file = ledger.run_path(PROJECT, repair_run_id)
        run_file.parent.mkdir(parents=True, exist_ok=True)
        crew._write_json(run_file, {"run_id": repair_run_id, "status": "promoted"})
        crew.pointer_path(repair_run_id).unlink()
        after = resumption.sweep(PROJECT, config=CONFIG, launcher=launcher)
    assert after["reviews"]["repaired"] == []
    assert len(calls) == 1


def test_a_clean_review_dispatches_no_repair(
    isolated_project: tuple[Path, Path, str],
) -> None:
    """The clean half: a review with no finding is not work, so nothing composes."""
    config_home, repo, head_sha = isolated_project
    _completed_pointer(config_home, repo)
    _store_review(head_sha, [])
    calls: list[dict] = []
    report = _sweep(calls)
    assert report["reviews"]["repaired"] == []
    assert calls == []


def test_a_live_worker_dispatches_no_repair(
    isolated_project: tuple[Path, Path, str],
) -> None:
    """A reviewed run already working is left to its own worker."""
    config_home, repo, head_sha = isolated_project
    _completed_pointer(config_home, repo, pid=os.getpid())
    _store_review(head_sha, FINDINGS)
    calls: list[dict] = []
    report = _sweep(calls)
    assert report["reviews"]["repaired"] == []
    assert calls == []


def test_a_resumed_worker_dispatches_no_repair(
    isolated_project: tuple[Path, Path, str],
) -> None:
    """A reviewed run whose worker was resumed on the same finding is left alone."""
    config_home, repo, head_sha = isolated_project
    _completed_pointer(config_home, repo)
    _store_review(head_sha, FINDINGS)
    _write_resume_stream()
    calls: list[dict] = []
    report = _sweep(calls)
    assert report["reviews"]["repaired"] == []
    assert calls == []


def test_a_pointer_gone_at_launch_dispatches_nothing(
    isolated_project: tuple[Path, Path, str], monkeypatch
) -> None:
    """A reviewed run whose pointer vanishes before the launch is not repaired."""
    config_home, repo, head_sha = isolated_project
    _completed_pointer(config_home, repo)
    _store_review(head_sha, FINDINGS)
    monkeypatch.setattr(recovery, "read_pointer", lambda _run_id: None)
    calls: list[dict] = []
    report = _sweep(calls)
    assert report["reviews"]["repaired"] == []
    assert calls == []


def test_a_promoted_reviewed_run_dispatches_nothing(
    isolated_project: tuple[Path, Path, str],
) -> None:
    """A reviewed run the ledger already holds is settled and not repaired."""
    config_home, repo, head_sha = isolated_project
    _completed_pointer(config_home, repo)
    _store_review(head_sha, FINDINGS)
    run_file = ledger.run_path(PROJECT, RUN_ID)
    run_file.parent.mkdir(parents=True, exist_ok=True)
    crew._write_json(run_file, {"run_id": RUN_ID, "status": "promoted"})
    calls: list[dict] = []
    report = _sweep(calls)
    assert report["reviews"]["repaired"] == []
    assert calls == []


def test_a_review_of_a_review_dispatches_nothing(
    isolated_project: tuple[Path, Path, str],
) -> None:
    """A review run's own findings open no further repair."""
    config_home, repo, head_sha = isolated_project
    _completed_pointer(config_home, repo, node_id="review-of-some-run")
    _store_review(head_sha, FINDINGS)
    calls: list[dict] = []
    report = _sweep(calls)
    assert report["reviews"]["repaired"] == []
    assert calls == []


@pytest.mark.parametrize("role", ["investigate", "test"])
def test_a_non_implement_role_dispatches_nothing(
    isolated_project: tuple[Path, Path, str], role: str
) -> None:
    """An investigate or test run's findings are not source a repair acts on."""
    config_home, repo, head_sha = isolated_project
    _completed_pointer(config_home, repo, role=role)
    _store_review(head_sha, FINDINGS)
    calls: list[dict] = []
    report = _sweep(calls)
    assert report["reviews"]["repaired"] == []
    assert calls == []


def test_a_review_of_a_repair_dispatches_nothing(
    isolated_project: tuple[Path, Path, str],
) -> None:
    """A repair's own findings do not open a further repair — the chain is bounded."""
    config_home, repo, head_sha = isolated_project
    _completed_pointer(config_home, repo, node_id="repair-of-a-reviewed-node-abcdef")
    _store_review(head_sha, FINDINGS)
    calls: list[dict] = []
    report = _sweep(calls)
    assert report["reviews"]["repaired"] == []
    assert calls == []


def test_record_only_findings_dispatch_nothing(
    isolated_project: tuple[Path, Path, str],
) -> None:
    """A finding that names only the fleet's own record grants no scope."""
    config_home, repo, head_sha = isolated_project
    _completed_pointer(config_home, repo)
    run_dir = Path(runs.run_dir(RUN_ID))
    _store_review(
        head_sha,
        [
            _finding(str(run_dir / "manifest.md"), "1", "the manifest is malformed"),
            _finding(
                str(run_dir / "gate.log"), "6", "the gate log ends without a verdict"
            ),
            _finding(
                "docs/evidence/fragments/some-plan/some-node.html",
                "12",
                "the fragment overstates the result",
            ),
        ],
    )
    calls: list[dict] = []
    report = _sweep(calls)
    assert report["reviews"]["repaired"] == []
    assert calls == []


def test_the_scope_drops_record_paths_but_keeps_source_paths(
    isolated_project: tuple[Path, Path, str],
) -> None:
    """A round mixing record and source findings is dispatched with source only."""
    config_home, repo, head_sha = isolated_project
    _completed_pointer(config_home, repo)
    run_dir = Path(runs.run_dir(RUN_ID))
    _store_review(
        head_sha,
        [
            _finding("reckon/crew/thing.py", "10", "off-by-one in the loop"),
            _finding(str(run_dir / "manifest.md"), "1", "reports the wrong count"),
        ],
    )
    calls: list[dict] = []
    report = _sweep(calls)
    repaired = report["reviews"]["repaired"]
    assert len(repaired) == 1
    scope = list(runs.read_pointer(repaired[0])["node"].get("write_paths") or [])
    assert "reckon/crew/thing.py" in scope
    assert str(run_dir / "manifest.md") not in scope
    assert not any(Path(path).is_absolute() or path.startswith("~") for path in scope)
