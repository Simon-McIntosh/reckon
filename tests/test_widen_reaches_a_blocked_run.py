"""crew widen reaches a run whose manifest reports blocked.

A worker that hits its fence writes ``status: blocked``, names the path it could
not write, and ends its turn. For a run launched through a backend the pointer's
phase is folded from the stream's terminal event, so a turn that ends normally
folds the phase to ``complete`` -- and ``crew widen``, deciding from that phase
alone, refused the run that was waiting for exactly this command. The only
remaining discharge was discard and redispatch, which throws away the session
that had diagnosed the change.

These cases drive the CLI runner against a synthesised run directory holding the
post-observe state: a pointer whose folded phase reads ``complete`` beside a
delivered manifest whose status reads ``blocked``. What is under test is the
command surface a coordinator reaches for, and the field it writes is the one
the promotion validator reads, so the assertions are made on the pointer bytes
on disk rather than on the reply alone.

The block is only half the rule the command states. The other half is that the
worker process has stopped, read from the process the pointer records rather than
from the phase: a run reporting a block while its worker still runs is a worker
writing against the boundary a widening would move, and the phase and the
manifest can both say blocked while it does. Every case here therefore asserts
the liveness read its fixture was built to produce before the command is
invoked, because a case whose pid read answered otherwise would pass or fail on
the wrong fact.

The state a case writes is isolated the way the sibling fence-widening cases
isolate theirs: ``RECKON_HOME`` points at a temporary home so a case's pointer
write stays inside the case, and the crew home the process resolved *before*
that substitution is watched byte for byte. The production pointer directory
cannot serve as that witness -- a live fleet rewrites it continuously, so a
difference there would say the machine is busy rather than that a case escaped.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from reckon.cli import main as cli_main
from reckon.crew.runs import (
    _process_start_time,
    _write_json,
    crew_home,
    pointer_path,
    record_process_alive,
)

BLOCKED_RUN_ID = "r-20260925T000000000000-widen-blocked-manifest"
COMPLETE_RUN_ID = "r-20260925T000000000000-widen-complete-manifest"
STALE_RUN_ID = "r-20260925T000000000000-widen-stale-manifest"
BLOCKED_PHASE_RUN_ID = "r-20260925T000000000000-widen-phase-blocked"
RUNNING_RUN_ID = "r-20260925T000000000000-widen-running-worker"
RUNNING_BEHIND_BLOCKED_ID = "r-20260925T000000000000-widen-running-behind-blocked"
STOPPED_RUN_ID = "r-20260925T000000000000-widen-stopped-worker"
DECLARED = "reckon/crew/runs.py"
MISSING = "reckon/crew/dispatch.py"
SESSION_ID = "s22-cli-20260925"
COMMITS = ["4f2a0b1c9d8e7f60"]

BLOCKED_MANIFEST = f"""orientation_worktree: {{worktree}}
orientation_base_sha: {"0" * 40}
orientation_write_paths: ["{DECLARED}"]
node: widen-reaches-a-blocked-run
status: blocked
checkpoint: the change needs {MISSING} and the fence does not carry it
commits: {COMMITS[0]}
changed_paths: ["{DECLARED}"]
tests: ran the focused gate: 12 passed, 0 failed
test_logs: ["/tmp/gate.log"]
blockers: write scope is missing {MISSING}; the change cannot land without it
evidence_inputs: none
follow_ons: none
"""

COMPLETE_MANIFEST = f"""orientation_worktree: {{worktree}}
orientation_base_sha: {"0" * 40}
orientation_write_paths: ["{DECLARED}"]
node: widen-reaches-a-blocked-run
status: complete
commits: {COMMITS[0]}
changed_paths: ["{DECLARED}"]
tests: ran the focused gate: 12 passed, 0 failed
test_logs: ["/tmp/gate.log"]
evidence_inputs: none
follow_ons: none
"""


def _tree_bytes(root: Path) -> dict[str, bytes] | None:
    """Every entry under ``root`` by relative path, with the file's own bytes.

    Directories carry a trailing slash so an empty directory appearing inside
    the tree is a difference rather than an invisible one. ``None`` stands for a
    root that does not exist, which is a state the comparison may see change.
    """
    if not root.exists():
        return None
    entries: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_dir():
            entries[relative + "/"] = b""
        elif path.is_file():
            entries[relative] = path.read_bytes()
    return entries


def _difference(before: dict[str, bytes] | None, after: dict[str, bytes] | None) -> str:
    """Name the paths that appeared, vanished or changed between two trees."""
    if before is None or after is None:
        return (
            f"the root existed={before is not None} before, {after is not None} after"
        )
    lines = []
    for relative in sorted(set(before) | set(after)):
        was, now = before.get(relative), after.get(relative)
        if was == now:
            continue
        if was is None:
            lines.append(f"  created: {relative}")
        elif now is None:
            lines.append(f"  removed: {relative}")
        else:
            lines.append(f"  changed: {relative} ({len(was)} -> {len(now)} bytes)")
    return "\n".join(lines)


class CrewHomeWatch:
    """A byte-for-byte witness of one crew home across a case's run."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.before = _tree_bytes(root)

    def assert_untouched(self) -> None:
        """Fail naming every path that moved under the watched root."""
        after = _tree_bytes(self.root)
        assert after == self.before, (
            f"the crew home {self.root} was written to by a case that did not "
            f"substitute it:\n{_difference(self.before, after)}"
        )


@pytest.fixture()
def crew_home_watch(isolated_reckon_home: Path) -> CrewHomeWatch:
    """Watch the crew home this process resolved before a case substituted one.

    The suite's own fixture is requested by name so its temporary home is
    already in place: the root under watch is where a case's pointer write lands
    if home resolution stops honouring the substitution the case makes, which is
    the escape the assertion exists to catch. Each case fixture below depends on
    this one, so the witness is always built before the substitution it guards.
    """
    root = crew_home()
    assert not root.is_relative_to(Path.home()), (
        f"the witness is watching the production directory {root}: the suite's "
        "temporary home is not in force, so a pass would be measuring the fleet"
    )
    return CrewHomeWatch(root)


def _exited_pid() -> int:
    """The pid of a process that has run to completion and been collected.

    The stop half of the rule is asserted against a pid the process table can no
    longer answer for, so the number is taken from a child that has been reaped
    rather than made up: a made-up number would exercise the same lookup while
    leaving it possible that no such process ever existed. The returned pid is
    checked by the case that uses it, which fails loudly rather than reporting a
    passing widen if the number has since been handed to another process.
    """
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    return child.pid


def _run_pointer(
    root: Path,
    run_id: str,
    *,
    phase: str,
    manifest: str | None,
    baseline: int | None = None,
    pid: int | None = None,
) -> dict:
    """Write one live pointer, its delivery and its dispatch-time baseline.

    ``baseline`` is the mtime the manifest path carried when the attempt was
    dispatched, which is what tells a fresh delivery from a status left behind
    by an earlier attempt at the same path. It is written explicitly here
    because the case controls which of the two it is synthesising.

    ``pid`` is the worker process the pointer records, and the case that passes
    one intends a specific liveness answer from it: the recorded start tick is
    read from the process table so the pointer carries the shape a dispatch
    writes, which is also the shape the pid-reuse check reads. A pid whose tick
    cannot be read is recorded without one, which is the unknown shape the
    lookup resolves on the probe alone.
    """
    worktree = root / "repo"
    manifest_path = root / f"{run_id}.md"
    if manifest is not None:
        manifest_path.write_text(manifest.format(worktree=worktree), encoding="utf-8")
        recorded_baseline = (
            manifest_path.stat().st_mtime_ns if baseline is None else baseline
        )
    else:
        recorded_baseline = 0
    pointer: dict[str, Any] = {
        "run_id": run_id,
        "project": "sample",
        "repo": str(worktree),
        "worktree": str(worktree),
        "base_sha": "0" * 40,
        "launch": "cli",
        "role": "implement",
        "phase": phase,
        "detail": "session ended its turn after reporting the block",
        "session_id": SESSION_ID,
        "commits": list(COMMITS),
        "node": {
            "id": "widen",
            "plan": "fixture",
            "section": "s22",
            "write_paths": [DECLARED],
        },
        "manifest_path": str(manifest_path),
        "manifest_baseline_mtime_ns": recorded_baseline,
    }
    if pid is not None:
        pointer["pid"] = pid
        start = _process_start_time(pid)
        if start is not None:
            pointer["pid_start_time"] = start
    _write_json(pointer_path(run_id), pointer)
    return pointer


@pytest.fixture()
def blocked_manifest_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, crew_home_watch: CrewHomeWatch
):
    """The measured shape: phase folded complete beside a blocked manifest.

    A fresh baseline of ``0`` is what a dispatch records when the delivery does
    not exist yet, so the manifest this case writes is this attempt's own -- the
    state a coordinator holds when the worker has reported its block and the
    phase has folded to complete.
    """
    monkeypatch.setenv("RECKON_HOME", str(_case_home(tmp_path)))
    (tmp_path / "repo").mkdir()
    return _run_pointer(
        tmp_path,
        BLOCKED_RUN_ID,
        phase="complete",
        manifest=BLOCKED_MANIFEST,
        baseline=0,
    )


@pytest.fixture()
def complete_manifest_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, crew_home_watch: CrewHomeWatch
):
    """The sibling: the run finished, and its manifest says so."""
    monkeypatch.setenv("RECKON_HOME", str(_case_home(tmp_path)))
    (tmp_path / "repo").mkdir()
    return _run_pointer(
        tmp_path,
        COMPLETE_RUN_ID,
        phase="complete",
        manifest=COMPLETE_MANIFEST,
        baseline=0,
    )


@pytest.fixture()
def complete_manifest_behind_a_blocked_phase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, crew_home_watch: CrewHomeWatch
):
    """A blocked phase whose manifest reports the work finished.

    The manifest is the run's own account of its state, so a terminal status
    there refuses on its own: there is no scope decision outstanding to widen
    for, whichever phase the mirror last folded.
    """
    monkeypatch.setenv("RECKON_HOME", str(_case_home(tmp_path)))
    (tmp_path / "repo").mkdir()
    return _run_pointer(
        tmp_path,
        BLOCKED_PHASE_RUN_ID,
        phase="blocked",
        manifest=COMPLETE_MANIFEST,
        baseline=0,
    )


@pytest.fixture()
def stale_manifest_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, crew_home_watch: CrewHomeWatch
):
    """A blocked manifest left at the path this attempt has not written yet.

    The baseline equals the file's own mtime, which is the state a resumed
    attempt is dispatched into: the path it will deliver to already carries the
    previous attempt's verdict. A live run must not be widened on the strength
    of that, so the status is not this attempt's to read.
    """
    monkeypatch.setenv("RECKON_HOME", str(_case_home(tmp_path)))
    (tmp_path / "repo").mkdir()
    return _run_pointer(
        tmp_path, STALE_RUN_ID, phase="complete", manifest=BLOCKED_MANIFEST
    )


@pytest.fixture()
def running_worker_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, crew_home_watch: CrewHomeWatch
):
    """A blocked manifest over a worker that is still running.

    The pointer's phase reads ``working`` and the manifest is this attempt's own,
    reporting blocked: the run is mid-turn, and the live process it records is
    the only thing that says so. The process is this test's own, which is the one
    pid a case can prove alive without spawning a second worker for the machine
    to carry.
    """
    monkeypatch.setenv("RECKON_HOME", str(_case_home(tmp_path)))
    (tmp_path / "repo").mkdir()
    return _run_pointer(
        tmp_path,
        RUNNING_RUN_ID,
        phase="working",
        manifest=BLOCKED_MANIFEST,
        baseline=0,
        pid=os.getpid(),
    )


@pytest.fixture()
def running_worker_behind_a_blocked_phase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, crew_home_watch: CrewHomeWatch
):
    """Both accounts of the block say blocked. The worker is running anyway.

    This is the case that separates the two halves of the rule: whatever the
    phase and the manifest say, a widening is refused while the process the
    pointer records still answers.
    """
    monkeypatch.setenv("RECKON_HOME", str(_case_home(tmp_path)))
    (tmp_path / "repo").mkdir()
    return _run_pointer(
        tmp_path,
        RUNNING_BEHIND_BLOCKED_ID,
        phase="blocked",
        manifest=BLOCKED_MANIFEST,
        baseline=0,
        pid=os.getpid(),
    )


@pytest.fixture()
def stopped_worker_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, crew_home_watch: CrewHomeWatch
):
    """The stopped twin of the running case, and the state widening exists for.

    The worker has exited and the manifest it left reports blocked, so the run is
    waiting on a scope decision the boundary can be moved for. The pid is one the
    process table has released, which is what "stopped" has to mean for the
    admission to be worth anything.
    """
    monkeypatch.setenv("RECKON_HOME", str(_case_home(tmp_path)))
    (tmp_path / "repo").mkdir()
    return _run_pointer(
        tmp_path,
        STOPPED_RUN_ID,
        phase="complete",
        manifest=BLOCKED_MANIFEST,
        baseline=0,
        pid=_exited_pid(),
    )


def _case_home(tmp_path: Path) -> Path:
    home = tmp_path / "config"
    home.mkdir()
    return home


def _widen(run_id: str, *paths: str):
    arguments = ["crew", "widen", "--run", run_id]
    for path in paths:
        arguments += ["--write-path", path]
    return CliRunner().invoke(cli_main, arguments)


def _read(run_id: str) -> dict:
    return json.loads(pointer_path(run_id).read_text())


def test_a_blocked_manifest_widens_a_run_whose_phase_folded_complete(
    blocked_manifest_run: dict, crew_home_watch: CrewHomeWatch
) -> None:
    """The falsifier for the phase-only check: this run is wideniable.

    Every half is asserted: the command accepts the run, the missing path lands
    in the fence the promotion validator reads, and the reply names the manifest
    status that authorised the write -- because the phase in that same reply
    still reads complete, which without the manifest status would look like a
    widening of a finished run.
    """
    before = _read(BLOCKED_RUN_ID)

    result = _widen(BLOCKED_RUN_ID, MISSING)

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["added"] == [MISSING]
    assert payload["manifest_status"] == "blocked"
    after = _read(BLOCKED_RUN_ID)
    assert after["node"]["write_paths"] == [DECLARED, MISSING]
    assert before["node"]["write_paths"] == [DECLARED]
    crew_home_watch.assert_untouched()


def test_the_widened_run_keeps_its_session_and_every_other_recorded_field(
    blocked_manifest_run: dict, crew_home_watch: CrewHomeWatch
) -> None:
    """The run resumes on its own session, which is the point of widening.

    A widening that granted the path while rewriting the session would satisfy
    the acceptance case and defeat the reason the command exists, so the session
    id is asserted unchanged against the pointer that was written before the run
    -- and every other field is required to be carried through as it was.
    """
    before = _read(BLOCKED_RUN_ID)

    result = _widen(BLOCKED_RUN_ID, MISSING)

    assert result.exit_code == 0, result.output
    after = _read(BLOCKED_RUN_ID)
    assert after["session_id"] == before["session_id"] == SESSION_ID
    assert {key: value for key, value in after.items() if key != "node"} == {
        key: value for key, value in before.items() if key != "node"
    }
    assert {
        key: value for key, value in after["node"].items() if key != "write_paths"
    } == {key: value for key, value in before["node"].items() if key != "write_paths"}
    crew_home_watch.assert_untouched()


def test_a_complete_manifest_is_refused(
    complete_manifest_run: dict, crew_home_watch: CrewHomeWatch
) -> None:
    """The sibling case: a finished run is refused and its pointer is left alone.

    The refusal is asserted on both halves -- a non-zero exit and a pointer
    directory whose bytes are what they were -- because a command that refuses
    in name while still rewriting the record would leave a coordinator believing
    the fence had not moved.
    """
    before = _tree_bytes(pointer_path(COMPLETE_RUN_ID).parent)

    result = _widen(COMPLETE_RUN_ID, MISSING)

    assert result.exit_code != 0, result.output
    assert MISSING not in result.output
    assert _read(COMPLETE_RUN_ID)["node"]["write_paths"] == [DECLARED]
    assert _tree_bytes(pointer_path(COMPLETE_RUN_ID).parent) == before
    crew_home_watch.assert_untouched()


def test_a_complete_manifest_is_refused_over_a_blocked_phase(
    complete_manifest_behind_a_blocked_phase: dict, crew_home_watch: CrewHomeWatch
) -> None:
    """The manifest is decisive when it reports the work ended.

    Reading the manifest alongside the phase must not become a way into runs
    that are not waiting on a scope decision at all: a run whose own account
    says complete or failed has nothing to widen for, even when something else
    last folded its phase to blocked. The refusal names the manifest rather than
    the phase, so a coordinator can see which account decided it.
    """
    result = _widen(BLOCKED_PHASE_RUN_ID, MISSING)

    assert result.exit_code != 0, result.output
    assert "in its own manifest" in result.output
    assert _read(BLOCKED_PHASE_RUN_ID)["node"]["write_paths"] == [DECLARED]
    crew_home_watch.assert_untouched()


def test_a_manifest_from_an_earlier_attempt_does_not_widen_the_run(
    stale_manifest_run: dict, crew_home_watch: CrewHomeWatch
) -> None:
    """A delivery left at the path by the anchor attempt is not this run's.

    A resumed attempt is dispatched at the same manifest path, so the previous
    attempt's terminal verdict is sitting exactly where the new one will land.
    Believing it would widen a live run against a block that has already been
    answered, so only a manifest written after the attempt's baseline counts.
    """
    result = _widen(STALE_RUN_ID, MISSING)

    assert result.exit_code != 0, result.output
    assert _read(STALE_RUN_ID)["node"]["write_paths"] == [DECLARED]
    crew_home_watch.assert_untouched()


def _assert_liveness(run_id: str, expected: bool | None) -> None:
    """Assert the read the command itself makes, before the command makes it.

    A case built to exercise the stop half is worth nothing if the pid it
    recorded does not answer as intended, and a passing widen would then report
    the wrong fact about it. The read is taken through the same helper the
    command uses, on the pointer as it is on disk.
    """
    alive = record_process_alive(_read(run_id))
    assert alive is expected, (
        f"the fixture's worker for {run_id} reads alive={alive}, not {expected}: "
        "the case would otherwise be measuring a different state"
    )


def test_a_blocked_manifest_does_not_widen_a_run_whose_worker_still_runs(
    running_worker_run: dict, crew_home_watch: CrewHomeWatch
) -> None:
    """A fresh block statement is not a stopped worker, and the process decides.

    The manifest is this attempt's own and reports blocked, which is the whole
    of the condition the phase-only refinement added -- so under a predicate that
    stops at the manifest this run is widened while its worker is mid-turn. The
    refusal is asserted on the reply and on the pointer bytes, and the pid the
    fixture recorded is asserted alive first, because the case is only about the
    liveness read if it really is.
    """
    _assert_liveness(RUNNING_RUN_ID, True)
    before = _tree_bytes(pointer_path(RUNNING_RUN_ID).parent)

    result = _widen(RUNNING_RUN_ID, MISSING)

    assert result.exit_code != 0, result.output
    assert "still running" in result.output
    assert "crew resume" in result.output or "exit" in result.output
    assert MISSING not in result.output
    assert _read(RUNNING_RUN_ID)["node"]["write_paths"] == [DECLARED]
    assert _tree_bytes(pointer_path(RUNNING_RUN_ID).parent) == before
    crew_home_watch.assert_untouched()


def test_a_running_worker_refuses_the_widening_the_phase_alone_would_allow(
    running_worker_behind_a_blocked_phase: dict, crew_home_watch: CrewHomeWatch
) -> None:
    """Both accounts say blocked; the worker says running, and the worker decides.

    Reading the block from the phase and the manifest covers a mirror that lags
    the run. It does not cover a worker whose phase is blocked because it
    recovered mid-stream, or one the coordinator is racing as it writes its last
    delivery, and those are the writers this refusal exists for. The run is
    documented as blocked by both sources and is still refused, which is only
    true if the process -- and not either field -- is what admits the widening.
    """
    _assert_liveness(RUNNING_BEHIND_BLOCKED_ID, True)
    before = _tree_bytes(pointer_path(RUNNING_BEHIND_BLOCKED_ID).parent)

    result = _widen(RUNNING_BEHIND_BLOCKED_ID, MISSING)

    assert result.exit_code != 0, result.output
    assert "still running" in result.output
    assert _read(RUNNING_BEHIND_BLOCKED_ID)["node"]["write_paths"] == [DECLARED]
    assert _tree_bytes(pointer_path(RUNNING_BEHIND_BLOCKED_ID).parent) == before
    crew_home_watch.assert_untouched()


def test_a_run_whose_worker_has_exited_still_widens_exactly_as_before(
    stopped_worker_run: dict, crew_home_watch: CrewHomeWatch
) -> None:
    """The stopped twin, unchanged: the admission is for this run, not against it.

    Every assertion of the case the manifest read was added for is repeated here
    against a pointer that records a worker the process table has released, so
    the stop half admits what the block half admits and refuses only the run
    whose worker can still write.
    """
    _assert_liveness(STOPPED_RUN_ID, False)

    result = _widen(STOPPED_RUN_ID, MISSING)

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["added"] == [MISSING]
    assert payload["manifest_status"] == "blocked"
    assert _read(STOPPED_RUN_ID)["node"]["write_paths"] == [DECLARED, MISSING]
    crew_home_watch.assert_untouched()


def test_the_witness_fires_on_a_substituted_root(tmp_path: Path) -> None:
    """The witness is shown to fire, on a root the production home never sees.

    A guard that never fires is indistinguishable from no guard, so the failure
    the witness exists to catch is made to happen here and the assertion is
    shown to name it. The write goes into a substitute root, and the production
    crew home is never a party to this case at all.
    """
    root = tmp_path / "substituted-crew-home"
    (root / "live").mkdir(parents=True)
    watch = CrewHomeWatch(root)

    watch.assert_untouched()  # nothing written yet: the witness stays quiet

    _write_json(
        root / "live" / f"{BLOCKED_RUN_ID}.json",
        {"run_id": BLOCKED_RUN_ID, "node": {"id": "widen"}},
    )

    with pytest.raises(AssertionError, match=f"live/{BLOCKED_RUN_ID}.json"):
        watch.assert_untouched()
