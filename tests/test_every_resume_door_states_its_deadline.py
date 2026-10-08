"""Every resume door writes the attempt's own time fence to the advice file.

A resumed worker reads its prompt from ``resume-{turn}-advice.txt``. The sweep
and the lane-change door compose that file carrying the plan's own prompt — the
advice restated with the time fence for the attempt now starting — so the
worker measures its clock from this attempt rather than from the first one.
Two hand-advice doors used to compose the file from the bare advice and drop the
fence: the MCP resume surface and ``reckon crew resume``. A repair driven
through either left the worker reading the inherited session's original
deadline, which is how runs refused mid-repair citing an already-expired
deadline.

Both doors now write the file through the same helper ``resumption._resume``
uses, so the fence cannot drift between the doors. Each is driven here with a
stubbed launcher: what would have launched is read back from the advice file at
the launcher's ``prompt_path``, which is exactly what the worker would read.

The declared negative control restores the bare-advice write on the shared
helper, so the ``reckon crew resume`` case must then turn red — the file the
worker reads would name no attempt deadline at all.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

import reckon.crew.dispatch_sessions as dispatch_sessions_module
import reckon.mcp as mcp_module
from reckon import cli as cli_module
from reckon import crew
from reckon.crew import resumption
from tests import test_resume_and_lane_change_follow_their_own_attempt as lane
from tests.test_resume_and_lane_change_follow_their_own_attempt import (  # noqa: F401
    crew_home,
    operator_home,
    repo,
)

pytestmark = pytest.mark.arms_watch_producer

# The declared mutation, printed verbatim as the red log's first line.
DECLARED_MUTATION = (
    "make the reckon crew resume door write the bare advice again; "
    "its test must turn red and the log is recorded"
)

MUTATION_ENV = "RECKON_RESUME_DOOR_NEGATIVE_CONTROL"

# The attempt's own launch instant, pinned so the fence the door compiles is
# deterministic and can be asserted against the attempt start rather than
# against the wall clock. The budget is the fixture node's own 25m.
ATTEMPT_LAUNCH = "2031-01-01T01:00:00+00:00"
ATTEMPT_DEADLINE = "2031-01-01T01:25:00Z"

FENCE_HEADER = "FENCE — TIME (resumed attempt)"
ADVICE = "the finding is answered in your own worktree; continue"


def _write_bare_advice(advice_path: Path, *, plan, advice: str) -> None:
    """The write the declared mutation restores: the bare advice, no fence."""
    advice_path.write_text(advice.rstrip("\n") + "\n", encoding="utf-8")


@pytest.fixture(autouse=True)
def _declared_negative_control(monkeypatch: pytest.MonkeyPatch) -> None:
    """Apply the declared mutation when its environment variable is set."""
    if os.environ.get(MUTATION_ENV) == "1":
        monkeypatch.setattr(resumption, "_write_resume_prompt", _write_bare_advice)


@pytest.fixture()
def _pinned_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the launch instant ``resume_plan`` composes the fence from."""
    monkeypatch.setattr(
        lane.dispatch_module, "_utc_now", lambda: ATTEMPT_LAUNCH
    )
    monkeypatch.setattr(dispatch_sessions_module, "_utc_now", lambda: ATTEMPT_LAUNCH)


class _CapturingSpawn:
    """Stands in for the spawn, recording the prompt the worker would read."""

    def __init__(self) -> None:
        self.prompt_text: str | None = None

    def __call__(self, plan, *, log_path, stderr_path, prompt_path) -> int:
        self.prompt_text = Path(prompt_path).read_text(encoding="utf-8")
        return 4_194_303


def _stub_flight(monkeypatch: pytest.MonkeyPatch) -> None:
    """Wire both doors' flight resolution to the fixture config.

    The subject is the advice file each door writes, not how the flight config
    resolves; the fixture repository has no flight file of its own.
    """
    monkeypatch.setattr(cli_module, "_resolved_flight", lambda *a, **k: lane.CONFIG)
    monkeypatch.setattr(
        mcp_module.flight_module,
        "resolve",
        lambda *a, **k: SimpleNamespace(config=lane.CONFIG),
    )


def _mcp_entry(**kwargs):
    """Drive the MCP ``crew`` tool exactly as a client call would."""
    tool = next(
        item
        for item in mcp_module.mcp._tool_manager.list_tools()
        if item.name == "crew"
    )
    return asyncio.run(tool.run(kwargs))


def _assert_advice_carries_its_own_deadline(text: str) -> None:
    """The worker's prompt carries the advice and one attempt deadline."""
    assert ADVICE in text, text
    assert text.count(FENCE_HEADER) == 1, text
    assert text.count(ATTEMPT_DEADLINE) == 1, text
    assert f"Launched {ATTEMPT_LAUNCH} UTC" in text, text


def test_the_mcp_resume_door_writes_the_attempts_own_deadline(
    repo: Path,  # noqa: F811 - imported fixture, requested by name
    crew_home: Path,  # noqa: F811 - imported fixture, requested by name
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _pinned_clock: None,
) -> None:
    """The MCP resume surface writes the plan prompt, fence restated."""
    _stub_flight(monkeypatch)
    run_id = "r-mcp-resume-door"
    lane._stopped_pointer(tmp_path, repo, run_id, backend="alpha")
    spawn = _CapturingSpawn()
    _stub_spawn(monkeypatch, spawn)

    result = _mcp_entry(action="resume", run_id=run_id, advice=ADVICE)

    assert result["ok"] is True, result
    assert spawn.prompt_text is not None, "the MCP door launched nothing"
    _assert_advice_carries_its_own_deadline(spawn.prompt_text)


def test_the_cli_resume_door_writes_the_attempts_own_deadline(
    repo: Path,  # noqa: F811 - imported fixture, requested by name
    crew_home: Path,  # noqa: F811 - imported fixture, requested by name
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _pinned_clock: None,
) -> None:
    """``reckon crew resume`` writes the plan prompt, fence restated."""
    _stub_flight(monkeypatch)
    run_id = "r-cli-resume-door"
    lane._stopped_pointer(tmp_path, repo, run_id, backend="alpha")
    spawn = _CapturingSpawn()
    _stub_spawn(monkeypatch, spawn)

    result = CliRunner().invoke(
        cli_module.main,
        ["crew", "resume", "--run", run_id, "--advice", ADVICE],
    )

    assert result.exit_code == 0, result.output
    assert spawn.prompt_text is not None, "the CLI door launched nothing"
    _assert_advice_carries_its_own_deadline(spawn.prompt_text)


def _stub_spawn(monkeypatch: pytest.MonkeyPatch, spawn: _CapturingSpawn) -> None:
    """Point both doors' spawn at the capturing stub.

    Both doors call ``crew_module._spawn`` where ``crew_module`` is the
    ``reckon.crew`` package, so patching that one attribute covers each door.
    """
    monkeypatch.setattr(crew, "_spawn", spawn)


if __name__ == "__main__":  # pragma: no cover - names the red-log recipe
    import sys

    sys.stdout.write(
        f"{DECLARED_MUTATION}\n"
        "apply by setting the bare-advice write on the shared helper "
        "(restoring the pre-fix door), then pytest this file\n"
    )
    os.environ[MUTATION_ENV] = "1"
