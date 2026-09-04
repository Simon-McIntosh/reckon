"""A lapsed provider refusal resumes itself, within four bounds.

Every pointer, stream and worktree here is built the way dispatch writes them,
under a temporary configuration home, and the refusal is a real recorded one
rather than a synthesised block — the whole point is that eligibility is read
from what a refused run actually leaves on disk. Nothing launches: the sweep
takes its launcher, so a resume is observed as the invocation it would spawn.
"""

from __future__ import annotations

import importlib
import json
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest

from reckon import cli as cli_module
from reckon import crew
from reckon.crew.resumption import (
    CONTINUE_ADVICE,
    hold_state,
    recovery_log_path,
    sweep,
)
from reckon.crew.runs import _write_json, pointer_path

PROJECT = "proj"
REFUSED_STREAM = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "backends"
    / "codex-usage-limit.jsonl"
)
# A pid the test can be sure is not running, so a resumed pointer still reads as
# a stopped run rather than a live one.
DEAD_PID = 4_194_303


def test_facade_child_module_attributes_are_modules() -> None:
    """A separately imported helper module cannot replace a public callable."""
    script = """
import json
from pathlib import Path
from types import ModuleType

import reckon.crew as crew

child_names = {
    path.stem for path in Path(crew.__path__[0]).glob("*.py")
} - set(crew._MODULE_EXPORTS)
violations = {
    name: type(value).__name__
    for name in sorted(child_names)
    if hasattr(crew, name)
    and not isinstance((value := getattr(crew, name)), ModuleType)
}
print(json.dumps(violations, sort_keys=True))
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(result.stdout) == {}


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home


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


def _stream_without_a_session(source: Path) -> str:
    """The same recorded refusal with its session id fields removed.

    Still a real refused stream — the refusal, its stated reset and every other
    event are the recorded ones — so the absence under test is an absence of
    the session and not of the refusal.
    """
    lines = []
    for line in source.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except ValueError:
            lines.append(line)
            continue
        # Both spellings: this dialect names it thread_id and another names it
        # session_id, and stripping only one leaves the id in place.
        event.pop("session_id", None)
        event.pop("thread_id", None)
        lines.append(json.dumps(event))
    return "\n".join(lines) + "\n"


def _refused_run(
    tmp_path: Path,
    run_id: str,
    *,
    worktree: bool = True,
    write_paths: tuple[str, ...] = ("reckon/one.py",),
    session_on_pointer: bool = False,
    session_in_stream: bool = True,
) -> dict:
    """A pointer for a run a provider refusal stopped, as dispatch leaves it."""
    directory = tmp_path / "runs" / run_id
    directory.mkdir(parents=True, exist_ok=True)
    stream = directory / "stream.jsonl"
    if session_in_stream:
        stream.write_bytes(REFUSED_STREAM.read_bytes())
    else:
        stream.write_text(_stream_without_a_session(REFUSED_STREAM), encoding="utf-8")
    tree = tmp_path / "trees" / run_id
    if worktree:
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
        "created_at": "2026-09-03T09:00:00Z",
        "log_path": str(stream),
        "manifest_path": str(manifest),
        "phase": "working",
        "node": {
            "id": run_id.rsplit("-", 1)[-1],
            "plan": "plan-a",
            "section": "§7",
            "time_budget": "30m",
            "write_paths": list(write_paths),
        },
    }
    if session_on_pointer:
        record["session_id"] = "sess-on-the-pointer"
    _write_json(pointer_path(run_id), record)
    return record


class _Launcher:
    """Stands in for the spawn, recording what would have been launched."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, plan, *, log_path, stderr_path, prompt_path) -> int:
        self.calls.append(
            {
                "prompt": Path(prompt_path).read_text(encoding="utf-8").strip(),
                "log_path": Path(log_path),
                "plan": plan,
            }
        )
        return DEAD_PID


def _clock_at(monkeypatch: pytest.MonkeyPatch, moment) -> None:
    """Make every clock in the decision read the moment the test states.

    The sweep is handed ``now``; the budget verdict a resume passes through
    reads its own. In production both are the wall clock and agree — a test
    that moves one and not the other is measuring a disagreement it invented.
    """
    from reckon import budget as budget_module
    from reckon.crew import resumption as resumption_module

    monkeypatch.setattr(budget_module, "_now", lambda now=None: now or moment)
    # The sweep's own clock too, because a surface that takes no ``now`` — the
    # command an operator runs — must read the same moment as one that does.
    monkeypatch.setattr(resumption_module, "_now", lambda now=None: now or moment)


def _real_crew_home(monkeypatch: pytest.MonkeyPatch) -> Path:
    with monkeypatch.context() as fresh:
        fresh.delenv("RECKON_HOME", raising=False)
        return crew.crew_home()


def test_a_lapsed_refusal_is_resumed_once_with_a_continue_advice(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The recovery this whole plan exists for, and it happens unattended."""
    run_id = "r-20260903T090000000000-node-a"
    _refused_run(tmp_path, run_id)
    after = _stated_reset() + timedelta(minutes=1)
    _clock_at(monkeypatch, after)
    launcher = _Launcher()

    report = sweep(PROJECT, launcher=launcher, now=after)

    assert [item["run_id"] for item in report["resumed"]] == [run_id]
    assert report["skipped"] == []
    resumed = report["resumed"][0]
    assert resumed["advice"] == CONTINUE_ADVICE
    assert "reset" in CONTINUE_ADVICE and "continue" in CONTINUE_ADVICE
    # The session came from the stream, which is the source a pointer carrying
    # no session id would otherwise hide.
    assert resumed["session_source"] == "stream"
    assert resumed["session_id"]
    assert len(launcher.calls) == 1
    assert launcher.calls[0]["prompt"] == CONTINUE_ADVICE
    assert resumed["session_id"] in json.dumps(launcher.calls[0]["plan"].as_dict())

    # What an operator reads afterwards, rather than inferring it from a run
    # that is suddenly alive again.
    recorded = [
        json.loads(line)
        for line in recovery_log_path(PROJECT).read_text(encoding="utf-8").splitlines()
    ]
    assert recorded[-1]["resumed"][0]["run_id"] == run_id
    assert not (_real_crew_home(monkeypatch) / "live" / f"{run_id}.json").exists()


def test_a_second_sweep_finds_nothing_to_do(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Idempotence is what lets something already running call this."""
    run_id = "r-20260903T091000000000-node-a"
    _refused_run(tmp_path, run_id)
    after = _stated_reset() + timedelta(minutes=1)
    _clock_at(monkeypatch, after)
    launcher = _Launcher()

    sweep(PROJECT, launcher=launcher, now=after)
    second = sweep(PROJECT, launcher=launcher, now=after)

    assert len(launcher.calls) == 1
    assert second["resumed"] == []


def test_a_run_refused_again_waits_for_the_new_holds_own_expiry(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One resume per hold, so a lane that refuses again gets no retry storm.

    Both halves are asserted: the stamp refuses a second resume for the hold
    that has already been answered, and a fresh refusal on the resumed turn is
    a new hold the sweep waits on rather than one it may retry.
    """
    run_id = "r-20260903T092000000000-node-a"
    _refused_run(tmp_path, run_id)
    reset = _stated_reset()
    _clock_at(monkeypatch, reset + timedelta(minutes=1))
    launcher = _Launcher()
    sweep(PROJECT, launcher=launcher, now=reset + timedelta(minutes=1))
    assert len(launcher.calls) == 1

    # The resumed turn is refused again, recorded on its own stream exactly as
    # the first refusal was.
    record = crew.read_pointer(run_id)
    resumed_log = Path(str(record["log_path"]))
    resumed_log.write_bytes(REFUSED_STREAM.read_bytes())
    held = sweep(PROJECT, launcher=launcher, now=reset - timedelta(minutes=1))
    assert len(launcher.calls) == 1
    assert [item["reason"] for item in held["skipped"]] == ["hold-in-force"]

    # And the hold that was already answered is not answered twice, even once
    # the clock is past it.
    record = crew.read_pointer(run_id)
    record["log_path"] = str(Path(str(record["manifest_path"])).parent / "stream.jsonl")
    _write_json(pointer_path(run_id), record)
    again = sweep(PROJECT, launcher=launcher, now=reset + timedelta(minutes=5))
    assert len(launcher.calls) == 1
    assert [item["reason"] for item in again["skipped"]] == [
        "already-resumed-for-this-hold"
    ]


def test_a_hold_still_in_force_is_never_resumed_onto(
    home: Path, tmp_path: Path
) -> None:
    """The sweep must never be what spends the last of a recovering quota."""
    run_id = "r-20260903T093000000000-node-a"
    _refused_run(tmp_path, run_id)
    launcher = _Launcher()

    report = sweep(
        PROJECT, launcher=launcher, now=_stated_reset() - timedelta(minutes=30)
    )

    assert launcher.calls == []
    assert report["resumed"] == []
    skip = report["skipped"][0]
    assert skip["reason"] == "hold-in-force"
    # The reason states the fact it was judged on, not just a verdict.
    assert "reset" in skip["detail"]


def test_a_missing_worktree_is_skipped_with_that_named(
    home: Path, tmp_path: Path
) -> None:
    """A resume has no working directory to start in, and that is worth saying."""
    run_id = "r-20260903T094000000000-node-a"
    _refused_run(tmp_path, run_id, worktree=False)
    launcher = _Launcher()

    report = sweep(
        PROJECT, launcher=launcher, now=_stated_reset() + timedelta(minutes=1)
    )

    assert launcher.calls == []
    skip = report["skipped"][0]
    assert skip["reason"] == "worktree-absent"
    assert run_id in skip["detail"]


def test_a_scope_another_live_run_claims_is_skipped(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Resuming into a scope somebody else owns is a collision, not a recovery."""
    run_id = "r-20260903T095000000000-node-a"
    _refused_run(tmp_path, run_id, write_paths=("reckon/shared.py",))
    other = "r-20260903T095500000000-node-b"
    _refused_run(tmp_path, other, write_paths=("reckon/shared.py",))
    live = crew.read_pointer(other)
    live["pid"] = 1
    _write_json(pointer_path(other), live)
    # The claimant is the run whose process is still alive; a stopped pointer
    # holds nothing, or every refused wave would be permanently unrecoverable.
    monkeypatch.setattr(
        "reckon.crew.resumption.process_alive",
        lambda pid: pid == 1,
    )
    launcher = _Launcher()

    report = sweep(
        PROJECT, launcher=launcher, now=_stated_reset() + timedelta(minutes=1)
    )

    assert launcher.calls == []
    skipped = {item["run_id"]: item for item in report["skipped"]}
    assert skipped[run_id]["reason"] == "scope-claimed-elsewhere"
    assert "reckon/shared.py" in skipped[run_id]["detail"]


def test_a_dry_run_reports_what_it_would_resume_and_resumes_nothing(
    home: Path, tmp_path: Path
) -> None:
    """The mode that makes the sweep safe to call from a test, and from a check."""
    run_id = "r-20260903T096000000000-node-a"
    _refused_run(tmp_path, run_id)
    launcher = _Launcher()

    report = sweep(
        PROJECT,
        launcher=launcher,
        dry_run=True,
        now=_stated_reset() + timedelta(minutes=1),
    )

    assert launcher.calls == []
    assert report["dry_run"] is True
    assert report["resumed"][0]["would_resume"] is True
    assert "auto_resume" not in crew.read_pointer(run_id)
    assert crew.read_pointer(run_id)["phase"] == "working"


def test_the_hold_with_no_stated_reset_ages_out_of_the_shelf_life(
    home: Path, tmp_path: Path
) -> None:
    """A refusal naming no reset is dated by the declared shelf life instead."""
    run_id = "r-20260903T097000000000-node-a"
    record = _refused_run(tmp_path, run_id)
    refusal = {"backend": "alpha", "limit_kind": "usage-limit", "resets_at": "unknown"}
    policy = {"evidence_shelf_life_minutes": 60.0}
    observed = hold_state(record, refusal, policy_block=policy)
    fresh = hold_state(
        record,
        refusal,
        policy_block=policy,
        now=None,
    )

    assert observed["in_force"] is True and fresh["in_force"] is True
    assert "shelf life" in observed["detail"]
    from reckon.crew.resumption import _refusal_observed_at

    lapsed = hold_state(
        record,
        refusal,
        policy_block=policy,
        now=_refusal_observed_at(record) + timedelta(minutes=90),
    )
    assert lapsed["in_force"] is False
    assert lapsed["signature"].startswith("observed:")


def test_the_follow_loop_sweeps_on_a_cadence_not_every_iteration(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Automatic means the loop already running calls it — sparingly.

    Asserted through the loop itself rather than through the helper, because
    the defect this closes is a capability that exists and nothing invokes.
    """
    swept: list[str] = []
    ticks = {"count": 0}
    elapsed = {"seconds": 0.0}

    def _clock() -> float:
        return elapsed["seconds"]

    def _sleeper(_interval: float) -> None:
        ticks["count"] += 1
        elapsed["seconds"] += 1.0

    class _Stop:
        def is_set(self) -> bool:
            return ticks["count"] >= 25

    runs_module = importlib.import_module("reckon.crew.runs")
    monkeypatch.setattr(runs_module, "producer_live", lambda _project: False)
    lines = list(
        cli_module._follow_watch_lines(
            PROJECT,
            poll_interval=0,
            sleeper=_sleeper,
            stop=_Stop(),
            sweep=lambda project, **_kw: swept.append(project),
            sweep_interval=10.0,
            clock=_clock,
        )
    )

    assert lines == []
    # Twenty-five iterations, one second apart, at a ten-second cadence: the
    # sweep runs on the first pass and then only when the cadence has elapsed.
    assert swept == [PROJECT, PROJECT, PROJECT]


# ── One session answer, whichever surface is asked ──────────────────────────
#
# The incident this closes turned on a bare null. A coordinator read
# `session_id: null` on five live pointers, took it for a verdict, and promoted
# all five — while resume had been recovering exactly that case from the stream
# for months. The null meant only that nothing had folded the stream in yet, and
# nothing anywhere expressed the difference between that and a genuine absence.


def _stream_session_of(run_id: str) -> str:
    """The id the run's own stream carries, read independently of the resolver."""
    from reckon import _backends

    record = crew.read_pointer(run_id)
    observation = _backends.observe_log(
        backend_name=str(record.get("backend") or ""),
        backend={"launch": "cli", "command": "codex"},
        log_path=Path(str(record["log_path"])),
    )
    return str(observation.session_id or "")


def _surface_answers(run_id: str, now) -> dict[str, dict]:
    """What each entry point says about one run's session, asked in turn.

    The mutating surface goes last on purpose: `crew observe` folds the stream
    into the pointer, so asking it first would change what every later surface
    is reading and turn one state into two.
    """
    from click.testing import CliRunner

    from reckon.crew.resumption import sweep as sweep_entry

    runner = CliRunner()
    swept = sweep_entry(PROJECT, dry_run=True, now=now)
    judged = (swept["resumed"] or swept["skipped"])[0]
    command = json.loads(
        runner.invoke(
            cli_module.main,
            ["crew", "resume-held", "--project", PROJECT, "--dry-run"],
        ).output
    )
    commanded = (command["resumed"] or command["skipped"])[0]
    listed = json.loads(
        runner.invoke(cli_module.main, ["crew", "list", "--project", PROJECT]).output
    )
    row = next(item for item in listed["runs"] if item["run_id"] == run_id)
    observed = json.loads(
        runner.invoke(cli_module.main, ["crew", "observe", "--run", run_id]).output
    )
    return {
        "sweep": judged,
        "command": commanded,
        "list": row,
        "observe": observed,
    }


def test_every_surface_answers_the_same_session_from_the_same_source(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pointer with no id and a stream with one: every surface says stream."""
    run_id = "r-20260904T030000000000-node-a"
    _refused_run(tmp_path, run_id)
    assert "session_id" not in crew.read_pointer(run_id)
    expected = _stream_session_of(run_id)
    assert expected
    after = _stated_reset() + timedelta(minutes=1)
    _clock_at(monkeypatch, after)

    answers = _surface_answers(run_id, after)

    assert {name: answer.get("session_id") for name, answer in answers.items()} == {
        "sweep": expected,
        "command": expected,
        "list": expected,
        "observe": expected,
    }
    assert {name: answer.get("session_source") for name, answer in answers.items()} == {
        "sweep": "stream",
        "command": "stream",
        "list": "stream",
        "observe": "stream",
    }
    assert not (_real_crew_home(monkeypatch) / "live" / f"{run_id}.json").exists()


def test_every_surface_states_the_same_absence(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An absence names what was consulted, so it cannot read as a verdict."""
    run_id = "r-20260904T031000000000-node-a"
    _refused_run(tmp_path, run_id, session_in_stream=False)
    after = _stated_reset() + timedelta(minutes=1)
    _clock_at(monkeypatch, after)

    answers = _surface_answers(run_id, after)

    for name in ("list", "observe"):
        assert answers[name]["session_id"] is None, name
        assert answers[name]["session_source"] is None, name
        resolution = answers[name]["session_resolution"]
        assert resolution["resolved"] is False
        # All three named, so a reader can tell this from a reading nobody took.
        assert resolution["consulted"] == ["pointer", "stream", "ledger"]
        assert "all three were consulted" in resolution["detail"]
    for name in ("sweep", "command"):
        assert answers[name]["reason"] == "no-recoverable-session", name
        assert "all three were consulted" in answers[name]["detail"], name


def test_a_promoted_run_still_answers_from_its_ledger_row(
    home: Path, tmp_path: Path
) -> None:
    """Promotion takes the pointer; the session it recorded still answers.

    This is the third source, and it is the one a coordinator needs after the
    operation that made the incident irreversible has already happened.
    """
    from reckon import ledger
    from reckon.crew.resumption import resolve_session

    run_id = "r-20260904T032000000000-node-a"
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    row = ledger.build_record(
        run_id=run_id,
        plan="plan-a",
        gate="not-run",
        agent={"backend": "alpha"},
        completed_at="2026-09-04T03:20:00Z",
        session_id="sess-in-the-ledger",
        outcome="promoted while the lane was refusing",
    )
    ledger.append_run(PROJECT, row, root=repo)

    answer = resolve_session(run_id, project=PROJECT, root=repo)

    assert answer["session_id"] == "sess-in-the-ledger"
    assert answer["source"] == "ledger"
    assert answer["resolved"] is True


def test_the_cadence_records_that_it_ran_even_when_it_finds_nothing(
    home: Path, tmp_path: Path
) -> None:
    """A running sweep and an absent sweep must not look identical.

    One merge left every follower on this workstation executing pre-merge code,
    unable to perform the recovery it appeared to have, and the pane said what
    it says when a fleet is simply quiet. The status record is what separates
    those two, so it is written on every sweep rather than only on the ones
    that found work.
    """
    from reckon.crew.resumption import sweep, sweep_status_path

    report = sweep(PROJECT, dry_run=True)

    assert report["checked"] == 0
    status = json.loads(sweep_status_path(PROJECT).read_text(encoding="utf-8"))
    assert status["project"] == PROJECT
    assert status["checked"] == 0
    assert status["resumed"] == 0
    assert status["swept_at"] == report["swept_at"]
    # Which process swept, because the failure this exposes is a follower
    # running code that cannot do the work.
    assert status["swept_by_pid"] > 0
    # And nothing went into the append-only record, which holds what happened
    # rather than that anything ran.
    assert not recovery_log_path(PROJECT).exists()
