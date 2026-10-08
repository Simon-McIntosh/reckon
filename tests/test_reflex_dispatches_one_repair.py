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
from reckon.crew import recovery_repair_dispatch
from reckon.crew import recovery_review_dispatch
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


def _finding(
    path: str, line: str, text: str, severity: str | None = None
) -> dict[str, str]:
    finding = {"file": path, "line": line, "text": text}
    if severity is not None:
        finding["severity"] = severity
    return finding


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


def _dispatch_direct(record: dict, monkeypatch) -> tuple[dict, list[dict]]:
    """Drive the reflex's repair dispatch itself, with the launch call stubbed.

    The sweep is the caller in production, but the guards the round passes
    through are read inside :func:`dispatch_repair_for_run` before the launch, so
    driving it directly isolates the refusal under test from whatever else a
    sweep additionally reaches. The dispatch is stubbed so the composed node is
    observed without cutting a worktree.
    """
    dispatch_module = importlib.import_module("reckon.crew.dispatch")
    calls: list[dict] = []

    def fake_dispatch(**kwargs):
        calls.append(kwargs)
        return {"run_id": "r-repair-stub"}

    monkeypatch.setattr(dispatch_module, "dispatch", fake_dispatch)
    with runs.follower_claim(PROJECT, "session-orchestrating", delivery="stream"):
        report = recovery.dispatch_repair_for_run(
            record, config=CONFIG, launcher=lambda *a, **k: os.getpid()
        )
    return report, calls


def _stub_resume(monkeypatch) -> list[dict]:
    """Replace the resume entry point the unpromoted path now uses.

    The round's composition reaches the reviewed run's own worker as advice
    rather than a new node's argv, so a case that read the composed brief, scope
    or suite arms now reads them off the advice the resume carries.
    """
    calls: list[dict] = []

    def fake_resume(run_id, record, *, config=None, launcher=None, advice=""):
        calls.append({"run_id": run_id, "advice": advice})
        return {"pid": os.getpid(), "turn": 1, "log_path": "resume-1.jsonl"}

    monkeypatch.setattr(resumption, "_resume", fake_resume)
    return calls


def _write_live_worker_record() -> None:
    """Record a live worker pid on the run, as the resumed supervisor would."""
    directory = Path(runs.run_dir(RUN_ID))
    directory.mkdir(parents=True, exist_ok=True)
    (directory / recovery.WORKER_RECORD_NAME).write_text(
        json.dumps({"pid": os.getpid()}), encoding="utf-8"
    )


def _scope_from_advice(advice: str) -> list[str]:
    """The write scope the resume advice names, parsed back to a path list."""
    line = next(
        item for item in advice.splitlines() if item.startswith("Write scope for")
    )
    return [path.strip() for path in line.split(":", 1)[1].split(",")]


def test_a_three_finding_review_resumes_exactly_once(
    isolated_project: tuple[Path, Path, str], monkeypatch
) -> None:
    """The positive half: the reflex composes and resumes the repair itself."""
    config_home, repo, head_sha = isolated_project
    _completed_pointer(config_home, repo)
    _store_review(head_sha, FINDINGS)
    resumed = _stub_resume(monkeypatch)
    _sweep([])

    assert len(resumed) == 1
    assert resumed[0]["run_id"] == RUN_ID
    advice = resumed[0]["advice"]
    # The brief names every finding by its content-derived id, and the scope the
    # round grants names every path the findings name: one resume, all three
    # findings, no coordinator command in between.
    for finding in FINDINGS:
        assert _expected_id(finding) in advice
        assert finding["file"] in advice

    # The scope is narrowed to the findings' own repository paths: no run
    # directory and no review-store path is granted, and no absolute path
    # appears.
    scope = _scope_from_advice(advice)
    assert set(scope) == {finding["file"] for finding in FINDINGS}
    assert not any(Path(path).is_absolute() or path.startswith("~") for path in scope)

    recorded = runs.read_pointer(RUN_ID)["repair_dispatch"]
    assert recorded["status"] == "resumed"
    assert recorded["run_id"] is None


def test_the_resume_is_the_reviewed_run_itself(
    isolated_project: tuple[Path, Path, str], monkeypatch
) -> None:
    """The repair is the reviewed run at the head the review read, not a new tree.

    An unpromoted run's repair reuses the run that already owns the worktree, so
    the reviewed head is inherent: the resumed worker is the one the review read;
    there is no separate base to cut.
    """
    config_home, repo, head_sha = isolated_project
    _completed_pointer(config_home, repo)
    _store_review(head_sha, FINDINGS)
    resumed = _stub_resume(monkeypatch)

    with _armed_fleet():
        resumption.sweep(PROJECT, config=CONFIG)

    assert len(resumed) == 1
    assert resumed[0]["run_id"] == RUN_ID
    assert all(finding["file"] in resumed[0]["advice"] for finding in FINDINGS)


def test_the_repair_keeps_the_reviewed_runs_whole_fence(
    isolated_project: tuple[Path, Path, str], monkeypatch
) -> None:
    """The round's scope carries the reviewed run's whole fence.

    The repair answers work on the reviewed run, so it is granted the fence that
    run already held: its test paths, which are the repair's gate, and the source
    paths it was dispensed. A finding naming only one of three fence paths must
    still carry all three into the round's scope, because the run's own grant is
    what the repair inherits rather than the single finding's citation.
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
    resumed = _stub_resume(monkeypatch)

    with _armed_fleet():
        resumption.sweep(PROJECT, config=CONFIG)

    assert len(resumed) == 1
    scope = _scope_from_advice(resumed[0]["advice"])
    assert "reckon/crew/thing.py" in scope
    assert "tests/test_reviewed_run.py" in scope
    assert "reckon/crew/new_module.py" in scope
    # No run directory, review-store path or absolute path is granted.
    assert not any(Path(path).is_absolute() or path.startswith("~") for path in scope)


def test_an_in_fence_figure_finding_reaches_the_scope(
    isolated_project: tuple[Path, Path, str], monkeypatch
) -> None:
    """A blocking finding under the run's fenced docs/figures subtree is work.

    The reviewed run was itself granted the figures subtree, so a finding under
    it is repairable however it is spelled; a path under ``docs/figures/`` is
    the fleet's own record only outside that fence. The composed scope must
    carry both the fence the run held and the finding beneath it, rather than
    filtering the finding to nothing and resuming the round with no scope.
    """
    config_home, repo, head_sha = isolated_project
    fence_dir = "docs/figures/multi-unit-limiter-wall/diiid-gate-frame-identity"
    finding_path = f"{fence_dir}/run.json"
    _completed_pointer(
        config_home,
        repo,
        node={
            "id": NODE_ID,
            "plan": "fixture",
            "section": "s2",
            "write_paths": [fence_dir, "docs/evidence/archive/wall-landed.html"],
        },
    )
    _store_review(
        head_sha,
        [_finding(finding_path, "212", "the frame identity is wrong", "blocking")],
    )
    resumed = _stub_resume(monkeypatch)

    with _armed_fleet():
        resumption.sweep(PROJECT, config=CONFIG)

    assert len(resumed) == 1
    scope = _scope_from_advice(resumed[0]["advice"])
    # The finding itself reaches the scope, and so does the fenced subtree it
    # lies under — the reviewed run's own grant, not the fleet's own record.
    assert finding_path in scope
    assert fence_dir in scope


def test_a_second_sweep_while_the_resumed_worker_is_live_resumes_nothing(
    isolated_project: tuple[Path, Path, str], monkeypatch
) -> None:
    """In flight only while the worker lives: a live resumed turn is left alone."""
    config_home, repo, head_sha = isolated_project
    _completed_pointer(config_home, repo)
    _store_review(head_sha, FINDINGS)
    resumed = _stub_resume(monkeypatch)

    with _armed_fleet():
        resumption.sweep(PROJECT, config=CONFIG)
        # The resumed turn is now running; its own worker record makes it live.
        _write_live_worker_record()
        resumption.sweep(PROJECT, config=CONFIG)

    assert len(resumed) == 1


def test_a_promoted_run_leaves_the_round_resuming_nothing(
    isolated_project: tuple[Path, Path, str], monkeypatch
) -> None:
    """Idempotence after promotion: a settled run is not resumed again."""
    config_home, repo, head_sha = isolated_project
    _completed_pointer(config_home, repo)
    _store_review(head_sha, FINDINGS)
    resumed = _stub_resume(monkeypatch)

    with _armed_fleet():
        resumption.sweep(PROJECT, config=CONFIG)
        # Promote the reviewed run: it lands a ledger row and its live pointer is
        # reconciled away. The round is settled, so no further resume fires.
        run_file = ledger.run_path(PROJECT, RUN_ID)
        run_file.parent.mkdir(parents=True, exist_ok=True)
        crew._write_json(run_file, {"run_id": RUN_ID, "status": "promoted"})
        crew.pointer_path(RUN_ID).unlink()
        resumption.sweep(PROJECT, config=CONFIG)

    assert len(resumed) == 1


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


def test_a_live_worker_record_dispatches_no_repair(
    isolated_project: tuple[Path, Path, str], monkeypatch
) -> None:
    """A reviewed run whose own worker record names a live pid is left alone.

    The pointer records no process here, so the only liveness the run carries is
    its worker record's — the supervisor that has exited leaves the pointer's pid
    silent while the work it started continues. The pid names this test's own
    child, still running when the reflex reads it and reaped when the case ends,
    so the refusal comes from the worker-record guard alone rather than from the
    pointer's process answer.
    """
    config_home, repo, head_sha = isolated_project
    record = _completed_pointer(config_home, repo)
    _store_review(head_sha, FINDINGS)
    child = subprocess.Popen(["sleep", "60"])
    try:
        _write_worker_record(pid=child.pid)
        report, calls = _dispatch_direct(record, monkeypatch)
        assert report["dispatched"] is False
        assert report["reason"] == "the reviewed run's worker is live"
        assert calls == []
    finally:
        child.terminate()
        child.wait()


def test_a_dead_worker_record_lets_the_repair_compose(
    isolated_project: tuple[Path, Path, str], monkeypatch
) -> None:
    """A reviewed run whose worker record names a dead pid is repaired in place.

    The worker record is the only liveness the run carries, so a reaped pid must
    not hold the round: the busy guard passes and the reflex composes the round's
    one repair and delivers it as a resume of the reviewed run itself — the run
    already owns the worktree and the commit claim a new node would be refused
    for, so no node is dispatched. This is the control the refusal above is read
    against — without it the refusal could pass on a fixture that never composed
    a repair at all.
    """
    config_home, repo, head_sha = isolated_project
    record = _completed_pointer(config_home, repo)
    _store_review(head_sha, FINDINGS)
    dead = subprocess.Popen(["sleep", "60"])
    dead_pid = dead.pid
    dead.terminate()
    dead.wait()
    _write_worker_record(pid=dead_pid)
    resumed = _stub_resume(monkeypatch)
    report, calls = _dispatch_direct(record, monkeypatch)

    assert report["resumed"] is True
    # The round was delivered as one resume of the reviewed run, carrying the
    # composed brief whose advice names every finding by its content-derived id.
    assert len(resumed) == 1
    assert resumed[0]["run_id"] == RUN_ID
    for finding in FINDINGS:
        assert _expected_id(finding) in resumed[0]["advice"]
    # No node was composed and dispatched: the resume replaced the dispatch.
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
    (monkeypatch.setattr(recovery_review_dispatch, "read_pointer", lambda _run_id: None), monkeypatch.setattr(recovery_repair_dispatch, "read_pointer", lambda _run_id: None))
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
    isolated_project: tuple[Path, Path, str], monkeypatch
) -> None:
    """A round mixing record and source findings grants source only."""
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
    resumed = _stub_resume(monkeypatch)
    _sweep([])

    assert len(resumed) == 1
    scope = _scope_from_advice(resumed[0]["advice"])
    assert "reckon/crew/thing.py" in scope
    assert str(run_dir / "manifest.md") not in scope
    assert not any(Path(path).is_absolute() or path.startswith("~") for path in scope)
