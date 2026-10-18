"""The review reflex's retry asks the route question before it re-queues.

The reflex composes an automatic review onto the local lane deterministically,
so a local lane whose router grants this session no worker re-queues the same
review there on every sweep: the lane grants the session no slot, the review is
queued on it, and the next sweep composes onto the same lane again.

The retry therefore asks the same route question the picker asks, with the
refusing lane left out of the offering, and the answer is the retry's. Each
assertion here is argued inside out. A retry must still re-queue on the local
lane when no other lane is offered to it, because a rule that always re-routed
would be a different defect with the same shape; and an answer that declines
must be reported rather than stepped over, because a retry that overrode Jev
would be the deterministic composition this question exists to replace.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from reckon import crew
from reckon.crew import budget_lift, recovery, runs
from reckon.crew.dispatch import WATCHER_LOAD_BOUND_SECONDS

# The reflex is gated by the watch admission, so these tests arm the producer
# the gate reads rather than accepting the suite-wide waiver, which would let
# every dispatch through and prove nothing about the refusal.
pytestmark = pytest.mark.arms_watch_producer


LOCAL_BACKEND = "alpha"
OTHER_BACKEND = "beta"
SESSION = "session-orchestrating"
PLAN = "fixture"
SECTION = "s2"


def _backend(command: str, *, lane_document: str, budget_group: str) -> dict:
    return {
        "launch": "cli",
        "command": command,
        "model": "some-model",
        "effort": "high",
        "sandbox": "worktree-full",
        "session_reuse": True,
        "time_budget": "25m",
        "lane_document": lane_document,
        "budget_group": budget_group,
    }


def _lane_document(path: Path, *, session_slots: float, lane_wide: float) -> str:
    """A lane document open lane-wide that grants this session its own share."""
    path.write_text(
        json.dumps(
            {
                "headroom": lane_wide,
                "admission": {
                    "worker_slots": lane_wide,
                    "observed_seconds": 900,
                    "sessions": {SESSION: {"worker_slots": session_slots}},
                    "verdict": "open",
                },
            }
        ),
        encoding="utf-8",
    )
    return str(path)


def _config(
    root: Path,
    *,
    local_slots: float = 0,
    review_excluded: bool = False,
) -> dict:
    config = {
        "default_backend": OTHER_BACKEND,
        "local_backend": LOCAL_BACKEND,
        "backends": {
            LOCAL_BACKEND: _backend(
                "clive",
                lane_document=_lane_document(
                    root / "lane-local.json", session_slots=local_slots, lane_wide=6
                ),
                budget_group="local-wallet",
            ),
            OTHER_BACKEND: _backend(
                "codex",
                lane_document=_lane_document(
                    root / "lane-codex.json", session_slots=4, lane_wide=8
                ),
                budget_group="codex-wallet",
            ),
        },
        "roles": {"implement": {}, "test": {}, "review": {}},
        "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
    }
    if review_excluded:
        config["review_excluded_backends"] = [OTHER_BACKEND]
    return config


# ── the lift in force on the metered lane ───────────────────────────────────


def _lift_record() -> dict:
    return {
        "id": "lift-1",
        "group": "codex-wallet",
        "form": "uncapped",
        "scope": "global",
        "pace_multiple": 1.0,
        "ends_at": None,
        "ends_on": "unknown",
        "remaining_seconds": None,
    }


def _write_lift(home: Path, *, granted: bool) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / budget_lift.LIFTS_LEAF).write_text(
        json.dumps({"version": 1, "lifts": [_lift_record()] if granted else []}),
        encoding="utf-8",
    )


# ── the picker, stubbed so the answer is the test's ─────────────────────────


def _offered(backend: str, lane: str, *, slots: float) -> dict:
    return {
        "backend": backend,
        "family": lane,
        "model": f"{backend}-model",
        "local": lane == "local",
        "worker_slots": slots,
    }


def _answer(*, action: str, backend: str | None, lane: str) -> dict:
    return {
        "action": action,
        "backend": backend,
        "family": lane,
        "model": None,
        "effort": None,
        "probabilities": {},
        "confidence": 0.5,
        "jev_model": "jev-test",
        "fallback_reason": None,
        "offered": [
            _offered(LOCAL_BACKEND, "local", slots=0),
            _offered(OTHER_BACKEND, "codex", slots=4),
        ],
        "excluded": [],
        "rendered_token_estimate": 0,
        "latency_ms": 1.0,
        "decision_source": "jev",
        "comment": "",
        "rendered_state": "{}",
        "questions": {},
        "template_digest": "t",
    }


def _stub_pick(monkeypatch: pytest.MonkeyPatch, answers: list[dict]) -> list[dict]:
    """Replace Jev's own pick and record the config every ask was made against."""
    seen: list[dict] = []

    class _Selection:
        def __init__(self, fields: dict) -> None:
            self._fields = fields

        def as_dict(self) -> dict:
            return dict(self._fields)

    def fake_pick(request, config, **kwargs):
        seen.append(dict(config))
        return _Selection(answers[min(len(seen) - 1, len(answers) - 1)])

    picker_module = importlib.import_module("reckon.crew.picker")
    monkeypatch.setattr(picker_module, "pick", fake_pick)
    return seen


# ── the project the reflex dispatches against ───────────────────────────────


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
    (plans / f"{PLAN}.html").write_text(
        '<meta name="docs-project" content="sample">'
        '<meta name="reckon-type" content="plan">'
        f'<meta name="plan-slug" content="{PLAN}">'
        f'<h2 id="{SECTION}">A held review is re-routed</h2>',
        encoding="utf-8",
    )
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "worker@example.invalid"],
        ["config", "user.name", "Worker"],
        ["add", "seed.txt", "skills", f"docs/plans/{PLAN}.html"],
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


def _wait_for_stopped_producer() -> None:
    deadline = time.monotonic() + WATCHER_LOAD_BOUND_SECONDS
    while time.monotonic() < deadline:
        if not crew.watch_state("sample")["watcher_live"]:
            return
        time.sleep(0.05)
    pytest.fail("watch producer did not release its seat")


def _release_watcher() -> None:
    if crew.watch_state("sample")["watcher_live"]:
        path = crew.watch_lock_path("sample")
        deadline = time.monotonic() + WATCHER_LOAD_BOUND_SECONDS
        while True:
            with path.open("rb") as handle:
                registration = crew._read_watch_record(handle)
            if registration.get("project") == "sample" and registration.get("pid"):
                break
            if time.monotonic() >= deadline:
                pytest.fail("watch producer did not publish its registration")
            time.sleep(0.05)
        recovery.unwatch("sample")
        _wait_for_stopped_producer()


def test_teardown_waits_for_the_producer_registration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    path = crew.watch_lock_path("sample")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    state = {"reads": 0, "stopped": False, "unwatch_calls": 0}

    def delayed_registration(_handle) -> dict:
        state["reads"] += 1
        if state["reads"] < 3:
            return {}
        return {"project": "sample", "pid": os.getpid()}

    def refuse_unpublished_registration(project: str) -> dict:
        state["unwatch_calls"] += 1
        with path.open("rb") as handle:
            registration = crew._read_watch_record(handle)
        if registration.get("project") != project:
            raise crew.CrewError("the locked registration names ''")
        state["stopped"] = True
        return {}

    monkeypatch.setattr(
        crew, "watch_state", lambda _project: {"watcher_live": not state["stopped"]}
    )
    monkeypatch.setattr(crew, "_read_watch_record", delayed_registration)
    monkeypatch.setattr(recovery, "unwatch", refuse_unpublished_registration)

    _release_watcher()

    assert state["reads"] >= 3
    assert state["unwatch_calls"] == 1


def _repo_head(repo: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _scoring_pointer(config_home: Path, repo: Path, run_id: str, *, head: str) -> dict:
    """A run awaiting review whose *local* lane already held its review once.

    The record is the one the reflex writes when the local lane raises the
    router's hold: the status a hold carries, the lane it held on, and the head
    the attempt composed for — so the retry reads a lane that took nothing from
    the run rather than one that dropped it.
    """
    manifest = config_home / "manifests" / (run_id + ".md")
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        "node: " + run_id + "\nstatus: complete\ncommits: " + run_id + "\n",
        encoding="utf-8",
    )
    record = {
        "run_id": run_id,
        "project": "sample",
        "repo": str(repo),
        "node": {"id": run_id, "plan": PLAN, "section": SECTION},
        "backend": LOCAL_BACKEND,
        "launch": "cli",
        "argv": ["clive"],
        "phase": "starting",
        "process_alive": False,
        "session": SESSION,
        "manifest_path": str(manifest),
        recovery.REVIEW_DISPATCH_FIELD: {
            "status": "lane-paused",
            "reason": "the local lane granted this session no worker",
            "run_id": None,
            "backend": LOCAL_BACKEND,
            "head": head,
            "at": "2026-10-13T08:00:00Z",
            "attempt": 1,
        },
    }
    crew._write_json(crew.pointer_path(run_id), record)
    return record


def _retry(record: dict, config: dict) -> dict:
    with runs.follower_claim("sample", SESSION, delivery="stream"):
        return recovery.dispatch_review_for_run(
            record, config=config, launcher=lambda *a, **k: os.getpid()
        )


def _recorded(run_id: str) -> dict:
    return runs.read_pointer(run_id)[recovery.REVIEW_DISPATCH_FIELD]


# ── the re-routed retry ─────────────────────────────────────────────────────


def test_a_held_review_is_re_routed_with_the_refusing_lane_excluded(
    tmp_path: Path, isolated_project: tuple[Path, Path], monkeypatch
) -> None:
    """The lifted case: one ask, the lane left out, both asks recorded.

    The local lane is open lane-wide but grants this session nothing, and the
    metered lane is under a lift so the reflex may offer it to a review. The
    retry must ask the route question with the refusing lane gone from the
    offering and carry the review onto the lane the answer named, rather than
    composing the same review onto the lane that already held it.
    """
    config_home, repo = isolated_project
    _write_lift(config_home, granted=True)
    record = _scoring_pointer(config_home, repo, "r-rerouted", head=_repo_head(repo))
    seen = _stub_pick(
        monkeypatch, [_answer(action="route", backend=OTHER_BACKEND, lane="codex")]
    )
    try:
        report = _retry(record, _config(tmp_path))
        assert report["dispatched"] is True
        assert report["backend"] == OTHER_BACKEND
        assert report["backend"] != LOCAL_BACKEND
        assert runs.read_pointer(report["review_run_id"])["backend"] == OTHER_BACKEND
    finally:
        _release_watcher()

    assert len(seen) == 1, "the retry did not ask the route question exactly once"
    assert LOCAL_BACKEND not in seen[0]["backends"], "the refusing lane was offered"
    assert OTHER_BACKEND in seen[0]["backends"]
    stored = _recorded("r-rerouted")
    assert [ask["backend"] for ask in stored["asks"]] == [
        LOCAL_BACKEND,
        OTHER_BACKEND,
    ], "both asks were not recorded"


def test_no_alternative_lane_still_re_queues_on_the_local_lane(
    tmp_path: Path, isolated_project: tuple[Path, Path], monkeypatch
) -> None:
    """The un-lifted case: the metered lane is review-excluded, so nothing changes.

    With the exclusion in force the reflex has no other lane to offer, so there
    is no question worth asking and the retry keeps the path it had: the same
    review composed onto the local lane, held there, and reported as held. A
    retry that re-routed here would send a review to a lane review routing
    deliberately removes.
    """
    config_home, repo = isolated_project
    _write_lift(config_home, granted=False)
    record = _scoring_pointer(config_home, repo, "r-held", head=_repo_head(repo))
    seen = _stub_pick(
        monkeypatch, [_answer(action="route", backend=OTHER_BACKEND, lane="codex")]
    )
    try:
        report = _retry(record, _config(tmp_path, review_excluded=True))
    finally:
        _release_watcher()

    assert seen == [], "the retry asked a question it had no alternative for"
    assert report["dispatched"] is False
    assert OTHER_BACKEND not in str(report.get("backend") or "")
    stored = _recorded("r-held")
    assert stored["backend"] == LOCAL_BACKEND
    assert "asks" not in stored


def test_an_answer_that_declines_is_not_overridden(
    tmp_path: Path, isolated_project: tuple[Path, Path], monkeypatch
) -> None:
    """Jev's own answer stands: a hold is reported, and nothing is composed.

    The refusal this guards against is a retry that asks the question, dislikes
    the answer, and steps back onto the lane anyway — which is the deterministic
    composition the question exists to replace, with an extra ask in front of
    it.
    """
    config_home, repo = isolated_project
    _write_lift(config_home, granted=True)
    record = _scoring_pointer(config_home, repo, "r-declined", head=_repo_head(repo))
    _stub_pick(monkeypatch, [_answer(action="hold", backend=None, lane="codex")])
    try:
        report = _retry(record, _config(tmp_path))
    finally:
        _release_watcher()

    assert report["dispatched"] is False
    assert report.get("awaiting_lane") is True
    assert LOCAL_BACKEND in report["reason"]
    assert not any(
        row["node"].get("id", "").startswith(recovery.REVIEW_NODE_PREFIX)
        for row in runs.list_live(project="sample")
    )
    stored = _recorded("r-declined")
    assert stored["status"] == "awaiting-lane"
    assert [ask["backend"] for ask in stored["asks"]] == [LOCAL_BACKEND, None]
    assert stored["asks"][0]["action"] == "held"
    assert stored["asks"][1]["action"] == "hold"
