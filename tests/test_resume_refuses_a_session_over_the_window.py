"""A resume refuses a session the lane's window cannot hold, before any attempt opens.

A resumed turn re-sends the session's whole context, so a session that has grown
past the lane's published input window dies at the endpoint with its attempt
already open: the endpoint refuses the prompt, the worker exits having done no
work, and the delivered manifest is left reading stale to promotion. The count
is not an estimate — it is the run's own last recorded request input, the figure
the stream reports for the last request the session served, cache included
because a resumed turn re-sends the cached context as well — and the window is
the smallest input the lane publishes, so the refusal rests on measurements
rather than on a prediction.

Both doors read it: a hand-typed ``crew resume`` builds the launcher's plan
directly, and the review-repair reflex resumes through ``_resume``, which is
that plan plus the attempt it opens. The refusal is raised before anything is
written, so a refused run keeps its classification and its manifest and gains
no attempt file, and the remedy is a fresh repair node, because the session
itself cannot be continued on this lane.

A lane publishing no window refuses nothing, and so does a run whose stream
recorded no usage at all: an unmeasured session is not a session known to be
too large, and the guard must fail open rather than hold work on a blank.

The declared mutation removes the window check from the resume door in a
scratch copy. The over-window cases then open their attempt and fail: the
reflex's stub launcher is called and its log written, and the hand-typed call
that was refused returns a plan.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from reckon.crew import recovery, resumption, runs
from reckon.crew.dispatch import resume_plan
from reckon.crew.node import CrewError
from reckon.crew.runs import _write_json, pointer_path
from tests import test_a_live_run_never_reads_dead as liveness

# The declared mutation, verbatim: the string the promotion audit matches
# against the red log's first line.
DECLARED_MUTATION = (
    "remove the window check; the over-window case then opens an attempt and fails"
)

PROJECT = "proj"
FOREIGN_HOST = "a-launcher-host-that-is-not-this-one"

# The lane's published input window, and two sessions on either side of it.
# Both sessions are sized the same way a real stream sizes one: the charged
# input is the direct input plus the cache read, which is what a resumed turn
# re-sends.
WINDOW = 200_000
INSIDE = 150_000
OVER = 250_000

WITHIN_RUN = "r-session-inside-the-lane-window"
OVER_RUN = "r-session-over-the-lane-window"

# Every run id this file mints, so a path named for one of these is a path only
# this test creates.
_RUN_IDS = (WITHIN_RUN, OVER_RUN)

LIVE_PID = 4_242_424

CONFIG = {
    "default_backend": "alpha",
    "backends": {
        "alpha": {
            "launch": "cli",
            "command": "codex",
            "sandbox": "worktree-full",
            "time_budget": "25m",
            "session_reuse": True,
            "usable_input_window": WINDOW,
        },
    },
    "roles": {"implement": {}},
    "budget": {
        "utilisation_ceiling_pct": 100,
        "resume_reserve_pct": 5,
        "exhausted_statuses": [],
    },
    "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
}


class _Launcher:
    """Stands in for the spawn, recording what was launched, not launching it.

    A real spawn creates the attempt's log file, so the stand-in does too: an
    attempt is the log it writes plus the turn it records, and a case asserting
    that no attempt opened must be able to see one open.
    """

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, plan, *, log_path, stderr_path, prompt_path) -> int:
        self.calls.append(
            {
                "log_path": log_path,
                "stderr_path": stderr_path,
                "prompt_path": prompt_path,
            }
        )
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        Path(log_path).write_text("", encoding="utf-8")
        return LIVE_PID


def _real_crew_home() -> Path:
    """The crew home a case writes to if its isolation is not in place."""
    return Path(os.path.expanduser("~")) / ".config" / "reckon" / "crew"


def _case_artifacts(crew_home: Path) -> list[Path]:
    """The paths this file's cases would leave under ``crew_home``."""
    return [
        *(crew_home / "runs" / run_id for run_id in _RUN_IDS),
        *(crew_home / "live" / f"{run_id}.json" for run_id in _RUN_IDS),
    ]


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Run against a temporary crew home, and prove the real one untouched.

    The proof is the absence of a live pointer and a run directory only this
    test creates, read after the case: a live fleet writes into the real home
    while the case runs, so a before-and-after reading of that home's own
    entries moves under the fleet's hand and cannot say who moved it.
    """
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    assert runs.crew_home().is_relative_to(tmp_path)
    yield config_home
    landed = [path for path in _case_artifacts(_real_crew_home()) if path.exists()]
    assert not landed, f"the real crew home carries this file's run paths: {landed}"


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """A throwaway repository, so a resume has a checkout to compose against."""
    root = tmp_path / "repo"
    root.mkdir()
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    for args in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "worker@example.invalid"],
        ["config", "user.name", "Worker"],
        ["add", "seed.txt"],
        ["commit", "-q", "-m", "chore: seed"],
    ):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
    return root


def _write_exit_record(run_id: str) -> None:
    """The supervisor's account of the end, written where its reader looks."""
    directory = runs.run_dir(run_id)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / recovery.EXIT_RECORD_NAME).write_text(
        json.dumps(
            {
                "run_id": run_id,
                "worker_pid": None,
                "launched_at": "2026-09-29T10:00:00Z",
                "exited_at": "2026-09-29T10:30:00Z",
                "exit_code": 0,
                "stream_records_seen": 4,
            }
        ),
        encoding="utf-8",
    )


def _stream(run_id: str, inputs: list[int]) -> Path:
    """The run's own stream, each entry the charged input of one request.

    The charge is published the way a live grammar publishes it: direct input
    plus the cache read, which together are the context the next request
    re-sends. The entries arrive in request order, so the last one is the count
    a resume of this session would carry.
    """
    directory = runs.run_dir(run_id)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "stream.jsonl"
    lines = [json.dumps({"type": "thread.started", "thread_id": "fixture"})]
    for charged in inputs:
        direct = charged // 3
        lines.append(
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "usage": {
                            "input_tokens": direct,
                            "cache_read_input_tokens": charged - direct,
                        }
                    },
                }
            )
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _pointer(
    tmp_path: Path,
    repo: Path,
    run_id: str,
    *,
    inputs: list[int],
) -> dict:
    """A stopped run whose session last carried ``inputs``, as a dispatch leaves it.

    Its end is observed twice over — a pid this host can answer for and found
    dead, and the supervisor's exit record — so nothing but the window can
    refuse a resume of it.
    """
    directory = runs.run_dir(run_id)
    directory.mkdir(parents=True, exist_ok=True)
    tree = tmp_path / f"{run_id}-tree"
    tree.mkdir(parents=True, exist_ok=True)
    manifest = directory / "manifest.md"
    manifest.write_text(
        "node: resume-refuses-a-session-over-the-window\nstatus: waiting\n",
        encoding="utf-8",
    )
    _write_exit_record(run_id)
    _stream(run_id, inputs)
    record = {
        "run_id": run_id,
        "project": PROJECT,
        "repo": str(repo),
        "worktree": str(tree),
        "launch": "cli",
        "argv": ["codex", "exec"],
        # The lane the run was launched on is the one the config declares the
        # window for, written as the name the config carries, so the rebuilt
        # settings and the configured entry name the same lane.
        "backend": "alpha",
        "role": "implement",
        "pid": liveness._absent_pid(),
        "pid_start_time": None,
        "process_alive": None,
        "session_id": "sess-on-the-pointer",
        "created_at": "2026-09-29T09:59:00+00:00",
        "launcher_host": FOREIGN_HOST,
        "log_path": str(directory / "stream.jsonl"),
        "manifest_path": str(manifest),
        "phase": "working",
        "attempt": 1,
        "node": {
            "id": run_id,
            "plan": "plan-a",
            "section": "",
            "time_budget": "30m",
            "write_paths": [],
        },
    }
    _write_json(pointer_path(run_id), record)
    return record


def _attempt_files(run_id: str) -> list[str]:
    return sorted(path.name for path in runs.run_dir(run_id).glob("resume-*"))


def test_a_session_inside_the_window_resumes(
    home: Path, tmp_path: Path, repo: Path
) -> None:
    """A session the lane can hold is resumed, and the last count is the one read.

    The earlier request in the stream is over the window and the last is inside
    it, so the case fails if the guard reads the first or the largest figure
    rather than the last recorded one, which is what a resumed turn carries.
    """
    _pointer(tmp_path, repo, WITHIN_RUN, inputs=[OVER, INSIDE])

    plan = resume_plan(WITHIN_RUN, "continue the same task", config=CONFIG)

    assert plan.resumed_session == "sess-on-the-pointer"


def test_a_session_over_the_window_is_refused_naming_the_count_and_the_window(
    home: Path, tmp_path: Path, repo: Path
) -> None:
    """The hand-typed door refuses before an attempt, naming both figures.

    The refusal names the count the session last carried and the lane's window,
    so a coordinator can size the work rather than guess why a resume it
    expected did not happen. Nothing is written: no attempt file, and the run's
    own classification and manifest are exactly as the dispatch left them.
    """
    before = _pointer(tmp_path, repo, OVER_RUN, inputs=[INSIDE, OVER])
    before_manifest = Path(before["manifest_path"]).read_text(encoding="utf-8")

    with pytest.raises(CrewError) as raised:
        resume_plan(OVER_RUN, "continue the same task", config=CONFIG)

    refusal = str(raised.value)
    assert str(OVER) in refusal, refusal
    assert str(WINDOW) in refusal, refusal
    assert "alpha" in refusal, refusal
    assert "repair node" in refusal, refusal
    assert _attempt_files(OVER_RUN) == []
    assert runs.read_pointer(OVER_RUN) == before
    assert Path(before["manifest_path"]).read_text(encoding="utf-8") == before_manifest


def test_the_reflex_refuses_the_session_before_it_opens_an_attempt(
    home: Path, tmp_path: Path, repo: Path
) -> None:
    """The review-repair reflex's own door refuses, and opens nothing.

    The reflex resumes through the sweep's ``_resume``, which is the launcher's
    plan plus the attempt's files and the turn it records. The refusal happens
    inside the plan, ahead of every write, so the launcher is never called and
    the run gains no attempt log — which is the difference between a refusal
    and a try that died at the endpoint.
    """
    before = _pointer(tmp_path, repo, OVER_RUN, inputs=[INSIDE, OVER])
    launcher = _Launcher()

    with pytest.raises(CrewError) as raised:
        resumption._resume(
            OVER_RUN,
            runs.read_pointer(OVER_RUN),
            config=CONFIG,
            launcher=launcher,
            advice="answer the review's findings",
        )

    refusal = str(raised.value)
    assert str(OVER) in refusal, refusal
    assert str(WINDOW) in refusal, refusal
    assert launcher.calls == []
    assert _attempt_files(OVER_RUN) == []
    assert runs.read_pointer(OVER_RUN) == before


def test_the_reflex_resumes_a_session_inside_the_window(
    home: Path, tmp_path: Path, repo: Path
) -> None:
    """The positive control: the same door opens an attempt it is allowed to.

    The session is one the lane can hold, so the reflex resumes it and the
    attempt is the one the run records — the turn, the advice the worker will
    read, and the log the launcher wrote. Without this case the refusals above
    would hold for a door that refuses everything.
    """
    _pointer(tmp_path, repo, WITHIN_RUN, inputs=[INSIDE])
    launcher = _Launcher()

    result = resumption._resume(
        WITHIN_RUN,
        runs.read_pointer(WITHIN_RUN),
        config=CONFIG,
        launcher=launcher,
        advice="answer the review's findings",
    )

    assert result["turn"] == 1
    assert result["pid"] == LIVE_PID
    assert len(launcher.calls) == 1
    assert Path(launcher.calls[0]["log_path"]).name == "resume-1.jsonl"
    assert Path(launcher.calls[0]["prompt_path"]).read_text(encoding="utf-8") == (
        "answer the review's findings\n"
    )
    resumed = runs.read_pointer(WITHIN_RUN)
    assert resumed["resumed_turn"] == 1
    assert resumed["attempt_kind"] == "resume"


def test_the_dry_run_predicts_the_same_refusal(
    home: Path, tmp_path: Path, repo: Path
) -> None:
    """A prediction reports the refusal a real resume would raise, not a resume.

    The sweep's dry run reports what the launcher would do. It consults the
    launcher's guards rather than restating them, so the over-window record
    must be reported as the launcher's own refusal, with its own message.
    """
    _pointer(tmp_path, repo, OVER_RUN, inputs=[INSIDE, OVER])

    mirror = resumption._launcher_refusal(runs.read_pointer(OVER_RUN), config=CONFIG)
    launcher_exc: BaseException | None = None
    try:
        resume_plan(OVER_RUN, "continue", config=CONFIG)
    except CrewError as exc:
        launcher_exc = exc

    assert launcher_exc is not None
    assert mirror is not None
    assert type(mirror) is type(launcher_exc)
    assert str(mirror) == str(launcher_exc)


def test_a_lane_publishing_no_window_refuses_nothing(
    home: Path, tmp_path: Path, repo: Path
) -> None:
    """An undeclared window is not a zero one, so the guard fails open.

    A lane that publishes no window has not said the session is too large, and
    a run whose stream recorded no usage has not been measured. Neither
    licenses a refusal: work held on a blank is the failure this direction
    avoids.
    """
    _pointer(tmp_path, repo, OVER_RUN, inputs=[INSIDE, OVER])
    config = json.loads(json.dumps(CONFIG))
    del config["backends"]["alpha"]["usable_input_window"]

    plan = resume_plan(OVER_RUN, "continue the same task", config=config)

    assert plan.resumed_session == "sess-on-the-pointer"

    # And the same lane's window declared, with a stream that never recorded a
    # request, refuses nothing either.
    _pointer(tmp_path, repo, WITHIN_RUN, inputs=[])
    plan = resume_plan(WITHIN_RUN, "continue the same task", config=CONFIG)
    assert plan.resumed_session == "sess-on-the-pointer"
