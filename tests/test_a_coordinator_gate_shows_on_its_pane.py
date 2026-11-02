"""A coordinator's own gate run draws a row on its own pane.

A coordinator that re-runs a 61-minute gate itself has no worker in flight, so
the pane reads as idle while the session is blocked on its own check. The two
verbs that run a gate — ``crew verify-gate`` and ``crew suite run`` — write a
durable record when they start and complete it when they end, and the producer
folds that record onto the owning session's stream.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import _plan_html, ledger
from reckon.cli import main as cli_main
from reckon.crew import gate_watch
from tests.helpers.git import git as _git

PROJECT = "proj"
PLAN = "plan-a"
RUN_ID = "r-20261029T040536910067-coordinator-gate"
SESSION = "sess-owner"
OTHER_SESSION = "sess-other"


def _write_resource(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    bare = (
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{PROJECT}">'
        f"<title>{state['slug']}</title>"
        '</head><body><main class="plan-doc"></main></body></html>\n'
    )
    path.write_text(_plan_html.write_state(bare, state), encoding="utf-8")


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A checkout with one promoted run, whose gate can be re-run from it."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    # The calling coordinator's own identity, which is what the gate row is
    # scoped to. A harness session id is the whole of it: the resolver needs no
    # process ancestry when the environment names one.
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SESSION)
    monkeypatch.delenv("CODEX_SESSION_ID", raising=False)
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    _write_resource(
        root / "docs" / "plans" / f"{PLAN}.html",
        {
            "type": "plan",
            "slug": PLAN,
            "title": "Plan A",
            "status": "active",
            "version": 0,
            "comments": {},
        },
    )
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
    ):
        _git(root, *arguments)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(root, "add", "seed.txt", "docs")
    _git(root, "commit", "-q", "-m", "test: seed repository")
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    ledger.append_run(
        PROJECT,
        {
            "run_id": RUN_ID,
            "gate": "passed",
            "gate_check": {"command": "true", "exit_status": 0, "log_digest": "x"},
        },
        root=root,
    )
    return root


def _document(home: Path) -> dict:
    return json.loads((home / "crew-gates.json").read_text(encoding="utf-8"))


def test_verify_gate_writes_a_start_record_then_completes_it(
    repository: Path, tmp_path: Path
) -> None:
    """The record exists while the gate runs, and carries the gate's exit
    status once it has run."""
    # The gate itself copies the document as it stands at the moment it is
    # executed, so the start record is evidence the gate's own command saw, not
    # a field this test inferred afterwards.
    command = 'cp "$RECKON_HOME/crew-gates.json" started.json'

    result = CliRunner().invoke(
        cli_main,
        [
            "crew",
            "verify-gate",
            "--project",
            PROJECT,
            "--run",
            RUN_ID,
            "--checkout-path",
            str(repository),
            "--command",
            command,
        ],
    )

    assert result.exit_code == 0, result.output
    started = json.loads((repository / "started.json").read_text(encoding="utf-8"))
    assert len(started["gates"]) == 1
    running = started["gates"][0]
    assert running["session"] == SESSION
    assert running["project"] == PROJECT
    assert running["run_id"] == RUN_ID
    assert running["ended_at"] is None

    finished = _document(tmp_path / "config")["gates"]
    assert len(finished) == 1
    assert finished[0]["id"] == running["id"]
    assert finished[0]["ended_at"] is not None
    assert finished[0]["exit_status"] == 0


def _sweep(document: Path, *, seen, rows: list) -> dict:
    return gate_watch.sweep(
        PROJECT,
        seen=seen,
        path=document,
        stream_path=document.parent / "stream.jsonl",
        append=lambda _path, batch: rows.extend(dict(row) for row in batch),
    )


def test_a_running_gate_draws_one_start_row_for_its_own_session(
    tmp_path: Path,
) -> None:
    """A producer sweep over a running gate emits one start row for the owning
    session, and none for another session."""
    document = tmp_path / "crew-gates.json"
    gate_watch.start(
        PROJECT,
        verb=gate_watch.VERIFY_GATE,
        checkout=tmp_path / "repo",
        command=".venv/bin/python -m pytest -q tests/",
        session=SESSION,
        run_id=RUN_ID,
        pid=os.getpid(),
        path=document,
    )
    rows: list = []

    _sweep(document, seen=None, rows=rows)

    starts = [row for row in rows if row.get("to_state") == gate_watch.GATE_RUNNING]
    assert len(starts) == 1
    assert starts[0]["session"] == SESSION
    assert starts[0]["node"] == gate_watch.VERIFY_GATE
    assert [row for row in rows if row.get("session") == OTHER_SESSION] == []


def test_a_second_sweep_repeats_nothing_and_an_end_draws_one_row(
    tmp_path: Path,
) -> None:
    """A second sweep emits no duplicate, and a sweep after the end emits one
    end row."""
    document = tmp_path / "crew-gates.json"
    record = gate_watch.start(
        PROJECT,
        verb=gate_watch.VERIFY_GATE,
        checkout=tmp_path / "repo",
        command=".venv/bin/python -m pytest -q tests/",
        session=SESSION,
        pid=os.getpid(),
        path=document,
    )
    rows: list = []
    seen = _sweep(document, seen=None, rows=rows)
    assert len(rows) == 1

    rows.clear()
    _sweep(document, seen=seen, rows=rows)
    assert rows == []

    gate_watch.end(record["id"], exit_status=1, path=document)
    rows.clear()
    _sweep(document, seen=seen, rows=rows)
    ends = [row for row in rows if row.get("to_state") == gate_watch.GATE_ENDED]
    assert len(ends) == 1
    assert ends[0]["session"] == SESSION
    assert "exit status 1" in ends[0]["detail"]


def test_a_gate_whose_process_is_gone_reads_as_ended(tmp_path: Path) -> None:
    """A record whose pid is gone with no end reads as ended, with no status."""
    document = tmp_path / "crew-gates.json"
    gate_process = subprocess.Popen(["sleep", "30"])
    gate_watch.start(
        PROJECT,
        verb=gate_watch.VERIFY_GATE,
        checkout=tmp_path / "repo",
        command="a gate that never wrote its end",
        session=SESSION,
        pid=gate_process.pid,
        path=document,
    )
    rows: list = []
    seen = _sweep(document, seen=None, rows=rows)
    assert len(rows) == 1
    assert len(gate_watch.running(path=document)) == 1

    gate_process.kill()
    gate_process.wait()

    assert gate_watch.running(path=document) == []
    rows.clear()
    _sweep(document, seen=seen, rows=rows)
    ends = [row for row in rows if row.get("to_state") == gate_watch.GATE_ENDED]
    assert len(ends) == 1
    assert "ended without reporting an exit status" in ends[0]["detail"]


def test_the_skill_names_the_verb_that_shows_a_long_gate() -> None:
    """The build skill names `verify-gate --command` for a long coordinator
    gate, so the verb that puts the gate on the pane is the one it recommends."""
    skill = (
        Path(__file__).resolve().parents[1] / "skills" / "reckon-build" / "SKILL.md"
    ).read_text(encoding="utf-8")

    assert "verify-gate --command" in skill
    assert "long" in skill.lower() and "gate" in skill.lower()
