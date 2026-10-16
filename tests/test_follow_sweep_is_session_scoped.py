# ruff: noqa: F811 - the scoring fixture is imported and also a test parameter.
"""A sweep acts on the session it is given, never on every session's runs.

A follower armed to observe another session ran the recovery sweep over the
observed session's runs, and in one case dispatched a review onto a peer's run
from the observing session's runtime. Both halves of the sweep crossed sessions:
the resume pass took every lapsed run from ``list_live`` whatever its session,
and the review-reflex pass resolved its session from the follower registration
this process happened to hold rather than from a session the caller named.

``sweep`` now takes the session it sweeps and gives it to both passes, so the
one mechanism scopes a follower's cadence, the resume-ready surface, the MCP
sweep action, and a caller holding no follower registration alike. Each half is
asserted against the run a stub records it was offered, and the negative that
distinguishes a scoped sweep from an unscoped one: a resume pass that ignored
the session touches both sessions' lapsed runs, and a reflex whose session was
not passed reviews both sessions' scoring runs.
"""

from __future__ import annotations

import socket
from datetime import timedelta
from pathlib import Path

import pytest

from reckon.crew.resumption import sweep
from reckon.crew.runs import _write_json, pointer_path
from reckon.crew_follow_commands import (
    _sweep_accepts_a_session,
    _sweep_lapsed_holds,
)
from tests.test_review_lane_follows_its_run import (  # noqa: F401 - fixture
    CONFIG,
    OWNING_BACKEND,
    _scoring_pointer,
    isolated_project,
)

PROJECT = "proj"
OWNING_SESSION = "session-owning"
OBSERVED_SESSION = "session-observed"
DEAD_PID = 4_194_303
REFUSED_STREAM = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "backends"
    / "codex-usage-limit.jsonl"
)


def _absent_pid() -> int:
    """A pid the kernel will never allocate: beyond the pid_max ceiling."""
    return int(Path("/proc/sys/kernel/pid_max").read_text().strip()) + 4096


def _stated_reset():
    """The reset moment the recorded refusal itself names."""
    from reckon.crew.recovery import _stream_refusal_block
    from reckon.crew.resumption import _parse_stamp

    block = _stream_refusal_block(
        {
            "launch": "cli",
            "backend": "alpha",
            "argv": ["codex"],
            "log_path": str(REFUSED_STREAM),
        }
    )
    assert block is not None, "the fixture must be a real recorded refusal"
    moment = _parse_stamp(block["resets_at"])
    assert moment is not None
    return moment


def _lapsed_run(tmp_path: Path, run_id: str, *, session: str) -> dict:
    """A pointer for a run a provider refusal stopped, owned by one session."""
    directory = tmp_path / "runs" / run_id
    directory.mkdir(parents=True, exist_ok=True)
    stream = directory / "stream.jsonl"
    stream.write_bytes(REFUSED_STREAM.read_bytes())
    tree = tmp_path / "trees" / run_id
    tree.mkdir(parents=True, exist_ok=True)
    manifest = directory / "manifest.md"
    manifest.write_text("node: node-a\nstatus: blocked\n", encoding="utf-8")
    record = {
        "run_id": run_id,
        "project": PROJECT,
        "repo": str(tmp_path / "repo"),
        "worktree": str(tree),
        "launch": "cli",
        "argv": ["codex", "exec"],
        "backend": "alpha",
        "role": "implement",
        "session": session,
        "created_at": "2026-09-03T09:00:00Z",
        "log_path": str(stream),
        "manifest_path": str(manifest),
        "phase": "working",
        # The end a lift rests on: this host checks the pid and finds it gone.
        "launcher_host": socket.gethostname(),
        "pid": _absent_pid(),
        "node": {
            "id": run_id.rsplit("-", 1)[-1],
            "plan": "plan-a",
            "section": "§7",
            "time_budget": "30m",
            "write_paths": ["reckon/one.py"],
        },
    }
    _write_json(pointer_path(run_id), record)
    return record


class _Launcher:
    """Stands in for the spawn, recording what would have been launched."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, plan, *, log_path, stderr_path, prompt_path) -> int:
        self.calls.append({"plan": plan, "log_path": Path(log_path)})
        return DEAD_PID


def _clock_at(monkeypatch: pytest.MonkeyPatch, moment) -> None:
    """Make every clock in the decision read the moment the test states."""
    from reckon import budget as budget_module
    from reckon.crew import resumption as resumption_module

    monkeypatch.setattr(budget_module, "_now", lambda now=None: now or moment)
    monkeypatch.setattr(resumption_module, "_now", lambda now=None: now or moment)


def test_a_session_sweep_resumes_only_that_sessions_lapsed_runs(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The owning session's lapsed run is resumed; the observed session's is not."""
    _lapsed_run(tmp_path, "r-owning", session=OWNING_SESSION)
    _lapsed_run(tmp_path, "r-observed", session=OBSERVED_SESSION)
    after = _stated_reset() + timedelta(minutes=1)
    _clock_at(monkeypatch, after)
    launcher = _Launcher()

    report = sweep(PROJECT, session=OWNING_SESSION, launcher=launcher, now=after)

    assert [item["run_id"] for item in report["resumed"]] == ["r-owning"]
    assert len(launcher.calls) == 1
    # The observed run was never considered: a resume pass that ignored the
    # session would judge both and resume both, so this figure separates a
    # scoped sweep from an unscoped one.
    assert report["checked"] == 1


def test_a_session_sweep_composes_only_that_sessions_review(
    isolated_project, monkeypatch: pytest.MonkeyPatch
) -> None:
    """From a process holding no registration, the named session's run is reviewed."""
    config_home, repo = isolated_project
    _scoring_pointer(
        config_home,
        repo,
        "r-mine",
        backend=OWNING_BACKEND,
        session=OWNING_SESSION,
    )
    _scoring_pointer(
        config_home,
        repo,
        "r-theirs",
        backend=OWNING_BACKEND,
        session=OBSERVED_SESSION,
    )

    offered: list[str] = []

    def record(pointer, **_kwargs):
        offered.append(str(pointer.get("run_id") or ""))
        return {
            "run_id": pointer.get("run_id"),
            "dispatched": True,
            "review_run_id": "r-review-of-" + str(pointer.get("run_id")),
        }

    monkeypatch.setattr(
        "reckon.crew.recovery_review_acceptance.dispatch_review_for_run", record
    )

    report = sweep(
        "sample",
        session=OWNING_SESSION,
        config=CONFIG,
        launcher=lambda *args, **kwargs: DEAD_PID,
    )

    assert offered == ["r-mine"], report["reviews"]


def test_the_followers_sweep_carries_the_session_it_serves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The follower's sweep forwards its owning session rather than dropping it."""
    calls: list[dict] = []

    def recorder(project, *, session=None, dry_run=False):
        calls.append({"project": project, "session": session, "dry_run": dry_run})
        return {}

    monkeypatch.setattr("reckon.crew.resumption.sweep", recorder)

    _sweep_lapsed_holds(PROJECT, session=OWNING_SESSION)

    assert calls == [{"project": PROJECT, "session": OWNING_SESSION, "dry_run": False}]


def test_a_single_argument_sweep_is_still_called_without_a_session() -> None:
    """The follower offers the session only to a sweep whose signature takes it."""
    assert _sweep_accepts_a_session(_sweep_lapsed_holds) is True
    assert _sweep_accepts_a_session(lambda project: {}) is False
    assert _sweep_accepts_a_session(lambda project, **_kw: {}) is True
