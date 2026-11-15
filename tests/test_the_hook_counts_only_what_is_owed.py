"""The hook's figures count only the obligations it lists.

A coordinator is told two things about one list: how many duties stand, and the
instruction to work them down. Both are read by a person deciding whether there
is anything to do; a count larger than the list says work is missing, and an
instruction printed over a session whose runs are all in flight or awaiting an
upstream section asks for work nobody at that session can do. A reported session
read "unreconciled runs: 3; work the list to empty" beside an empty list, where
the three were two in-flight workers and one request awaiting upstream -- runs
the closure calls unreconciled, none of them duties.

The last cases are the other direction: a session holding no live work is told
nothing about a producer that is absent, and the same session with one live run
is told, so the silence is a reading of the fleet rather than a hook that
stopped speaking.

Every case builds a synthesised configuration home and repository, so no reader
reaches a real checkout or the operator's own crew home.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reckon.crew import runs
from reckon.crew.obligations import obligations as obligations_view
from reckon.hooks.coordinator_obligations import format_checklist
from tests.helpers.git import git as _git

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOK = REPO_ROOT / "reckon" / "hooks" / "coordinator_obligations.py"
PROJECT = "owed-count-fixture"
SESSION = "owed-count-session"
OBSERVED_AT = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
WORK_INSTRUCTION = "work the list to empty"
# A pid no process can hold, so the drive's session resolves through the
# registration's own name rather than an ambient harness identity.
_ABSENT_HARNESS_PID = 2_147_483_647


@pytest.fixture()
def config_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(home / "mounts.json"))
    return home


@pytest.fixture()
def repository(tmp_path: Path, config_home: Path) -> Path:
    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "seed.txt"),
        ("commit", "-q", "-m", "test: seed owed-count fixture"),
    ):
        _git(root, *arguments)
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


def _row(kind: str, *, run_id: str, age: int = 30, **extra: object) -> dict:
    """One duty row of the shape the snapshot hands the hook."""
    row = {
        "kind": kind,
        "run_id": run_id,
        "node": f"{run_id}-node",
        "age_seconds": age,
        "next_command": f"reckon crew resume --run {run_id}",
    }
    row.update(extra)
    return row


def _payload(*rows: dict, count: int | None = None, unreconciled: int = 0) -> dict:
    """A payload whose summary is the derivation's own, as a stale one arrives.

    ``count`` and ``unreconciled`` are written into the summary rather than left
    to ``len(rows)`` because the defect is a summary that disagrees with the
    rows: the hook's figures must come from the rows it prints.
    """
    return {
        "project": PROJECT,
        "session": SESSION,
        "obligations": list(rows),
        "summary": {
            "count": len(rows) if count is None else count,
            "oldest_age_seconds": max(
                (int(row["age_seconds"]) for row in rows), default=0
            ),
            "unreconciled_runs": unreconciled,
        },
    }


def _rendered(payload: dict) -> str:
    return format_checklist(payload, limit_resets=[])


def _write_pointer(
    repository: Path,
    tmp_path: Path,
    *,
    run_id: Path | str,
    phase: str = "working",
    **extra: object,
) -> None:
    """Record one live pointer for the fixture session, in ``phase``."""
    root = tmp_path / "manifests"
    root.mkdir(parents=True, exist_ok=True)
    manifest = root / f"{run_id}.md"
    manifest.write_text(
        f"node: {run_id}\nstatus: in-progress\n", encoding="utf-8"
    )
    head = _git(repository, "rev-parse", "HEAD")
    runs._write_json(
        runs.pointer_path(str(run_id)),
        {
            "run_id": str(run_id),
            "project": PROJECT,
            "session": SESSION,
            "repo": str(repository),
            "worktree": str(repository),
            "base_sha": head,
            "process_alive": False,
            "role": "implement",
            "manifest_path": str(manifest),
            "phase": phase,
            "node": {
                "id": str(run_id),
                "plan": "fixture-plan",
                "section": "fixture-section",
                "time_budget": "20m",
                "write_paths": ["seed.txt"],
            },
            **extra,
        },
    )


def _drive(repository: Path, *, mode: str = "prompt") -> subprocess.CompletedProcess[str]:
    """Run the hook as the harness does, against the temporary home."""
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(REPO_ROOT)
    environment["CLAUDE_PID"] = str(_ABSENT_HARNESS_PID)
    payload = {
        "session_id": SESSION,
        "cwd": str(repository),
        "hook_event_name": "UserPromptSubmit",
    }
    return subprocess.run(
        [sys.executable, str(HOOK), "--hook", mode],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=environment,
        check=False,
    )


def _injected(completed: subprocess.CompletedProcess[str]) -> str:
    """The context one prompt drive injected, or empty when it stayed quiet."""
    assert completed.returncode == 0, completed.stderr
    assert completed.stderr == "", completed.stderr
    if not completed.stdout.strip():
        return ""
    return json.loads(completed.stdout)["hookSpecificOutput"]["additionalContext"]


def test_an_empty_list_prints_no_instruction_to_work_it() -> None:
    """The reported shape: a count of unreconciled runs beside an empty list.

    Three runs the closure calls unreconciled -- two in flight and one awaiting
    upstream -- are not three duties, and a session told to work a list it was
    shown empty has been told work is owed and shown nothing to do.
    """
    rendered = _rendered(_payload(unreconciled=3))

    assert "0 outstanding" in rendered, rendered
    assert WORK_INSTRUCTION not in rendered, rendered
    assert "unreconciled runs" not in rendered, rendered


def test_a_waiting_only_list_prints_no_instruction_to_work_it() -> None:
    """A request awaiting upstream is a wait, so no instruction to work it."""
    rendered = _rendered(
        _payload(
            _row("run-queued", run_id="run-upstream"),
            count=1,
            unreconciled=3,
        )
    )

    assert "no action shortens them" in rendered, rendered
    assert "run-upstream" not in rendered, rendered
    assert WORK_INSTRUCTION not in rendered, rendered


def test_the_header_counts_the_rows_this_render_prints() -> None:
    """A summary count larger than the list must not be repeated as its size."""
    rendered = _rendered(
        _payload(_row("blocked", run_id="run-blocked"), count=4, unreconciled=4)
    )
    header, trailer = rendered.splitlines()[0], rendered.splitlines()[-2]

    assert "1 outstanding" in header, header
    assert "4 outstanding" not in header, header
    assert trailer.startswith("unreconciled runs: 1;"), trailer


def test_the_trailer_counts_the_obligations_the_render_lists() -> None:
    """Two duties beside a closure count of nine are two.

    One waiting row is listed as a wait, so the count that names the list's size
    counts the rows printed: the actionable one in full, the waiting one on the
    collapsed line.
    """
    rendered = _rendered(
        _payload(
            _row("blocked", run_id="run-blocked", age=600),
            _row("review-queued", run_id="run-queued-review", age=60),
            count=9,
            unreconciled=9,
        )
    )

    assert "2 outstanding" in rendered.splitlines()[0], rendered
    assert "unreconciled runs: 2;" in rendered, rendered
    assert "no action shortens them" in rendered, rendered


def test_an_in_flight_session_is_told_to_work_nothing(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reported fleet: two workers in flight and one request upstream.

    The derivation calls all three unreconciled, and lists only the upstream
    request, as a wait. The render must print no instruction to work the list,
    and its figure must be the one row it lists.
    """
    import reckon.crew.obligations as derivation

    monkeypatch.setattr(derivation, "_utc_now", lambda: OBSERVED_AT)
    _write_pointer(repository, tmp_path, run_id="run-worker-one", phase="working")
    _write_pointer(repository, tmp_path, run_id="run-worker-two", phase="working")

    view = obligations_view(PROJECT, SESSION)
    assert view["summary"]["unreconciled_runs"] == 2, view["summary"]
    assert view["obligations"] == [], view["obligations"]

    rendered = _rendered(view)
    assert WORK_INSTRUCTION not in rendered, rendered

    _write_pointer(
        repository, tmp_path, run_id="run-upstream-request", phase="awaiting-upstream"
    )
    view = obligations_view(PROJECT, SESSION)
    assert view["summary"]["unreconciled_runs"] == 3, view["summary"]
    assert len(view["obligations"]) == 1, view["obligations"]

    rendered = _rendered(view)
    assert "1 outstanding" in rendered.splitlines()[0], rendered
    assert WORK_INSTRUCTION not in rendered, rendered


def test_a_session_with_no_runs_and_no_producer_is_told_nothing(
    repository: Path, tmp_path: Path
) -> None:
    """A session that dispatched nothing has no obligations to be stale about.

    The line offered ``watch --ensure`` at every prompt to a session whose fleet
    it never joined. The second drive is the same reading with one live run, so
    the silence is shown to be the fleet's answer rather than a hook that
    stopped speaking: an arm with work still names the remedy.
    """
    with runs.follower_registration(PROJECT, SESSION):
        pass

    idle = _drive(repository)
    assert idle.returncode == 0
    assert idle.stdout == "", idle.stdout
    assert idle.stderr == "", idle.stderr

    _write_pointer(repository, tmp_path, run_id="run-live-one", phase="working")
    working = _drive(repository)

    line = _injected(working)
    assert "no producer" in line, line
    assert f"reckon crew watch --ensure --project {PROJECT}" in line, line


@pytest.mark.parametrize(
    ("phase", "extra"),
    [
        ("launch-failed", {}),
        ("complete", {"manifest_path": None}),
        ("stopped", {"promoted_revision": "0" * 40}),
    ],
)
def test_a_settled_run_never_earns_the_absent_producer_line(
    repository: Path,
    tmp_path: Path,
    phase: str,
    extra: dict,
) -> None:
    """A pointer that never launched, or ended owing nothing, is not live work.

    The fleet's population rule excludes both populations, so a session whose
    pointers are all in one of them holds nothing a producer was publishing and
    is told nothing. Each case is driven separately, so a silence in one cannot
    stand for the others.
    """
    with runs.follower_registration(PROJECT, SESSION):
        pass
    _write_pointer(repository, tmp_path, run_id=f"run-{phase}", phase=phase, **extra)

    completed = _drive(repository)

    assert completed.returncode == 0
    assert completed.stdout == "", completed.stdout


def test_an_ended_run_owing_a_promotion_still_earns_the_line(
    repository: Path, tmp_path: Path
) -> None:
    """The exclusion is the settled population, not every ended pointer.

    A completed run holding its delivery manifest owes a promotion, which is
    work its coordinator may need the absent producer for; the same pointer
    without the manifest earns silence in the case above.
    """
    with runs.follower_registration(PROJECT, SESSION):
        pass
    _write_pointer(
        repository,
        tmp_path,
        run_id="run-complete-unpromoted",
        phase="complete",
    )

    line = _injected(_drive(repository))

    assert "no producer" in line, line
    assert f"reckon crew watch --ensure --project {PROJECT}" in line, line
