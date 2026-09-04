"""The MCP surface can resume a held run and ask for a session id.

Every pointer and stream here is built the way dispatch writes them, under a
temporary configuration home, so eligibility is read from what a refused run
actually leaves on disk rather than from a synthesised shortcut. Nothing is
launched for real: the spawn is monkeypatched, and what would have been
launched is asserted from the recorded call.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

import reckon.mcp as mcp_module
from reckon.crew.runs import _write_json, crew_home, pointer_path, run_dir
from reckon.doccheck import audit_file

PROJECT = "recover-proj"
USAGE_LIMIT_STREAM = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "backends"
    / "codex-usage-limit.jsonl"
)
# A pid guaranteed not to belong to a running process, so a resumed pointer
# still reads as stopped rather than live.
DEAD_PID = 4_194_303


def _tool_entry(name: str):
    """Return the tool registered on the client-facing MCP surface."""
    return next(
        item for item in mcp_module.mcp._tool_manager.list_tools() if item.name == name
    )


def _invoke_tool(name: str, **kwargs):
    """Exercise FastMCP argument validation and dispatch like a client call."""
    return asyncio.run(_tool_entry(name).run(kwargs))


def _crew_entry(**kwargs):
    return _invoke_tool("_crew", **kwargs)


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home


def _real_crew_home(monkeypatch: pytest.MonkeyPatch) -> Path:
    with monkeypatch.context() as fresh:
        fresh.delenv("RECKON_HOME", raising=False)
        return crew_home()


def _stopped_run(
    tmp_path: Path,
    run_id: str,
    *,
    session_on_pointer: bool = False,
    stream_path: Path = USAGE_LIMIT_STREAM,
    pid: int | None = DEAD_PID,
) -> dict:
    """A pointer for a run whose worker has stopped, as dispatch leaves it."""
    directory = tmp_path / "runs" / run_id
    directory.mkdir(parents=True, exist_ok=True)
    stream = directory / "stream.jsonl"
    stream.write_bytes(stream_path.read_bytes())
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
        "created_at": "2026-09-03T09:00:00Z",
        "log_path": str(stream),
        "manifest_path": str(manifest),
        "phase": "working",
        "node": {
            "id": run_id.rsplit("-", 1)[-1],
            "plan": "plan-a",
            "section": "§7",
            "time_budget": "30m",
            "write_paths": ["reckon/one.py"],
        },
    }
    if pid is not None:
        record["pid"] = pid
    if session_on_pointer:
        record["session_id"] = "sess-on-the-pointer"
    _write_json(pointer_path(run_id), record)
    # Dispatch creates the run's own directory (under crew_home, distinct from
    # the tmp_path scratch area above) before a first attempt ever runs; a
    # resume assumes it is already there.
    run_dir(run_id).mkdir(parents=True, exist_ok=True)
    return record


class _FakeSpawn:
    """Stands in for the real spawn, recording what would have launched."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, plan, *, log_path, stderr_path, prompt_path) -> int:
        self.calls.append(
            {
                "prompt": Path(prompt_path).read_text(encoding="utf-8").strip(),
                "plan": plan,
            }
        )
        return DEAD_PID


def test_resume_reattaches_a_session_already_on_the_pointer(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = "r-20260903T090000000000-node-a"
    _stopped_run(tmp_path, run_id, session_on_pointer=True)
    spawn = _FakeSpawn()
    monkeypatch.setattr(mcp_module.crew_module, "_spawn", spawn)

    result = _crew_entry(
        action="resume", run_id=run_id, advice="the limit has reset; continue"
    )

    assert result["ok"] is True
    assert result["session_id"] == "sess-on-the-pointer"
    assert result["session_source"] == "pointer"
    assert len(spawn.calls) == 1
    assert spawn.calls[0]["prompt"] == "the limit has reset; continue"
    assert result["resumed_session"] == "sess-on-the-pointer"


def test_resume_reattaches_a_session_only_the_stream_carries(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pointer carrying no session id is not evidence the run is unresumable."""
    from datetime import timedelta

    from reckon import budget as budget_module
    from reckon.crew.recovery import _stream_refusal_block
    from reckon.crew.resumption import _parse_stamp

    run_id = "r-20260903T091000000000-node-a"
    record = _stopped_run(tmp_path, run_id, session_on_pointer=False)
    # Resuming re-observes the stream, which carries the fixture's own
    # usage-limit refusal; move the budget verdict's clock past its stated
    # reset so the resume is judged on session recovery, not on a hold this
    # test did not set out to exercise.
    block = _stream_refusal_block(record)
    assert block is not None
    after = _parse_stamp(block["resets_at"]) + timedelta(minutes=1)
    monkeypatch.setattr(budget_module, "_now", lambda now=None: now or after)
    spawn = _FakeSpawn()
    monkeypatch.setattr(mcp_module.crew_module, "_spawn", spawn)

    result = _crew_entry(
        action="resume", run_id=run_id, advice="the limit has reset; continue"
    )

    assert result["ok"] is True
    assert result["session_source"] == "stream"
    assert result["session_id"] == "01a0635f-62a3-7283-a81b-61cd39bedb60"
    assert len(spawn.calls) == 1

    recorded = mcp_module.crew_module.read_pointer(run_id)
    assert not (_real_crew_home(monkeypatch) / "live" / f"{run_id}.json").exists()
    assert recorded["session_id"] == "01a0635f-62a3-7283-a81b-61cd39bedb60"


def test_resume_of_a_live_process_is_refused_like_the_cli(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = "r-20260903T092000000000-node-a"
    _stopped_run(tmp_path, run_id, session_on_pointer=True, pid=os.getpid())
    spawn = _FakeSpawn()
    monkeypatch.setattr(mcp_module.crew_module, "_spawn", spawn)

    result = _crew_entry(
        action="resume", run_id=run_id, advice="the limit has reset; continue"
    )

    assert result["ok"] is False
    assert result["error"] == "crew_error"
    assert "live process" in result["detail"]
    assert "observe or stop it before resuming" in result["detail"]
    assert spawn.calls == []


def test_resume_needs_advice_and_a_run_id(home: Path) -> None:
    missing_advice = _crew_entry(action="resume", run_id="r-x")
    assert missing_advice["ok"] is False
    assert missing_advice["error"] == "missing_advice"

    missing_run = _crew_entry(action="resume", advice="continue")
    assert missing_run["ok"] is False
    assert missing_run["error"] == "missing_run_id"


def test_session_answers_the_source_when_only_the_pointer_carries_it(
    home: Path, tmp_path: Path
) -> None:
    run_id = "r-20260903T093000000000-node-a"
    _stopped_run(tmp_path, run_id, session_on_pointer=True)

    result = _crew_entry(action="session", run_id=run_id)

    assert result["ok"] is True
    assert result["resolved"] is True
    assert result["session_id"] == "sess-on-the-pointer"
    assert result["source"] == "pointer"


def test_session_answers_the_source_when_only_the_stream_carries_it(
    home: Path, tmp_path: Path
) -> None:
    run_id = "r-20260903T094000000000-node-a"
    _stopped_run(tmp_path, run_id, session_on_pointer=False)

    result = _crew_entry(action="session", run_id=run_id)

    assert result["ok"] is True
    assert result["resolved"] is True
    assert result["source"] == "stream"
    assert result["session_id"] == "01a0635f-62a3-7283-a81b-61cd39bedb60"


def test_session_states_the_absence_rather_than_a_bare_null(
    home: Path, tmp_path: Path
) -> None:
    """No id anywhere is reported as a stated absence naming every source."""
    run_id = "r-20260903T095000000000-node-a"
    directory = tmp_path / "runs" / run_id
    directory.mkdir(parents=True, exist_ok=True)
    empty_stream = directory / "stream.jsonl"
    empty_stream.write_text('{"type":"turn.started"}\n', encoding="utf-8")
    record = {
        "run_id": run_id,
        "project": PROJECT,
        "repo": str(tmp_path / "repo"),
        "launch": "cli",
        "argv": ["codex", "exec"],
        "backend": "alpha",
        "log_path": str(empty_stream),
        "phase": "working",
    }
    _write_json(pointer_path(run_id), record)

    result = _crew_entry(action="session", run_id=run_id)

    assert result["ok"] is True
    assert result["resolved"] is False
    assert result["session_id"] is None
    assert result["source"] is None
    assert "pointer" in result["detail"]
    assert "stream" in result["detail"]
    assert "ledger" in result["detail"]


def test_session_needs_a_run_id(home: Path) -> None:
    result = _crew_entry(action="session")
    assert result["ok"] is False
    assert result["error"] == "missing_run_id"


def test_sweep_dry_run_reports_what_it_would_resume(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import timedelta

    from reckon.crew.recovery import _stream_refusal_block
    from reckon.crew.resumption import _parse_stamp

    run_id = "r-20260903T096000000000-node-a"
    _stopped_run(tmp_path, run_id, session_on_pointer=False)
    block = _stream_refusal_block(
        {
            "launch": "cli",
            "backend": "alpha",
            "argv": ["codex"],
            "log_path": str(USAGE_LIMIT_STREAM),
        }
    )
    assert block is not None
    reset_moment = _parse_stamp(block["resets_at"])
    after = reset_moment + timedelta(minutes=1)
    import reckon.crew.resumption as resumption_module

    # The hold decision reads resumption's own clock, not the budget module's;
    # the fixture's stated reset is in the future relative to wall-clock time,
    # so the test must move this clock rather than the ambient one.
    monkeypatch.setattr(resumption_module, "_now", lambda now=None: now or after)
    spawn = _FakeSpawn()
    monkeypatch.setattr(mcp_module.crew_module, "_spawn", spawn)
    monkeypatch.setattr(resumption_module, "_spawn", spawn)

    result = _crew_entry(action="sweep", project=PROJECT, dry_run=True)

    assert result["ok"] is True
    assert result["dry_run"] is True
    assert [item["run_id"] for item in result["resumed"]] == [run_id]
    assert result["resumed"][0]["would_resume"] is True
    assert result["resumed"][0]["session_source"] == "stream"
    assert spawn.calls == []


def test_sweep_needs_a_project(home: Path) -> None:
    result = _crew_entry(action="sweep")
    assert result["ok"] is False
    assert result["error"] == "missing_project"


def test_invalid_action_is_a_readable_refusal(home: Path) -> None:
    result = mcp_module._crew_recover("discard")
    assert result["ok"] is False
    assert result["error"] == "invalid_action"


def test_resume_of_an_unknown_run_is_a_readable_refusal(home: Path) -> None:
    result = _crew_entry(action="resume", run_id="r-does-not-exist", advice="continue")
    assert result["ok"] is False
    assert result["error"] == "crew_error"
    assert "r-does-not-exist" in result["detail"]


def test_real_config_home_is_untouched(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The isolated home takes every write; the shared real one sees none.

    A live workstation runs other agents against the real config home while
    this test executes, so a full directory snapshot comparison would race
    against their genuine writes. Scoping to this test's own run id avoids
    that race while still proving nothing leaked outside the isolated home.
    """
    real_home = _real_crew_home(monkeypatch)
    run_id = "r-20260903T097000000000-node-a"
    real_pointer = real_home / "live" / f"{run_id}.json"
    real_run_dir = real_home / "runs" / run_id
    assert not real_pointer.exists()
    assert not real_run_dir.exists()

    _stopped_run(tmp_path, run_id, session_on_pointer=True)
    spawn = _FakeSpawn()
    monkeypatch.setattr(mcp_module.crew_module, "_spawn", spawn)
    _crew_entry(action="resume", run_id=run_id, advice="continue")
    _crew_entry(action="session", run_id=run_id)
    _crew_entry(action="sweep", project=PROJECT, dry_run=True)

    assert not real_pointer.exists()
    assert not real_run_dir.exists()


def test_single_project_roadmap_defaults_to_summary_and_raw_is_explicit(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = {
        "project": PROJECT,
        "completion": {
            "plans": 3,
            "completed": 1,
            "pending": 2,
            "lifecycle_completion_pct": 33.3,
            "implementation_pct": 50.0,
        },
        "ready_now": [{"slug": "ready"}],
        "blocked": [{"slug": "blocked"}],
        "deferred": [],
        "wiring_findings": [{"severity": "warn", "code": "sample"}],
        "critical_path": {"plans": ["ready", "blocked"], "hours": 2.0},
        "schedule": {},
    }
    monkeypatch.setattr(
        mcp_module, "_discover_project", lambda *args: {"inventory": [], "sprints": []}
    )
    monkeypatch.setattr(
        mcp_module,
        "read_plan",
        lambda *args: ({"projects": [], "active_sprint_id": None}, 0),
    )
    monkeypatch.setattr(mcp_module, "list_followups_across", lambda *args, **kwargs: [])
    monkeypatch.setattr(mcp_module, "build_roadmap", lambda *args, **kwargs: raw)

    summary = _invoke_tool("_roadmap", project=PROJECT)
    lossless = _invoke_tool("_roadmap", project=PROJECT, view="raw")

    assert summary["view"] == "summary"
    assert summary["ready"] == 1
    assert summary["blocked"] == 1
    assert summary["finding_counts"]["total"] == 1
    assert summary["critical_path"] == raw["critical_path"]
    assert lossless == {"project": PROJECT, "view": "raw", "data": raw}


def test_audit_tool_validates_one_document_like_the_cli(
    home: Path, tmp_path: Path
) -> None:
    document = tmp_path / "docs" / "authored.html"
    document.parent.mkdir()
    document.write_text(
        "<!doctype html><html><head>"
        '<meta name="docs-project" content="recover-proj">'
        '<meta name="reckon-type" content="plan">'
        '<meta name="plan-slug" content="authored">'
        '<meta name="plan-title" content="Authored">'
        "<title>Authored</title></head>"
        '<body><main class="plan-doc"><p>**markdown**</p></main></body></html>',
        encoding="utf-8",
    )
    expected = [
        {"severity": item.severity, "code": item.code, "message": item.message}
        for item in audit_file(document, project=PROJECT)
    ]

    result = _invoke_tool("_audit", project=PROJECT, path=str(document))

    assert result["path"] == str(document)
    assert result["findings"] == expected
    assert result["finding_counts"]["total"] == len(expected)
    assert result["ok"] is False
