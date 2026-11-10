"""The oh-my-pi dialect: its argv, and its stream read against a recorded run.

Every parser case here reads the stream recorded in
``~/.config/reckon/crew/reports/reckon/omp-headless-contract.md``, kept beside
the codex stream at ``docs/research/fixtures/omp-stream.jsonl``. That fixture is
the note's four verbatim
records — the session header, the tool-call announcement, the tool result and
the settled assistant message carrying the run's token usage — together with
the surrounding event kinds the note names, the one truncated field (the last
record's millisecond tail) completed. Its token figures, tool call and final
message are the recorded ones.

Two of the note's findings would each pass silently if the parser were written
the obvious way, so both are held here:

* the harness reports an all-zero usage record on the *first* assistant message
  of a turn and the real totals on the last, so a parser reading the first
  reports a run that spent nothing;
* it writes plain prose to the same stream as the JSON records, so a parser
  that raises on a non-JSON line dies on a healthy run.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from reckon import _backends, _worker_fence

FIXTURES = Path(__file__).resolve().parents[1] / "docs" / "research" / "fixtures"
STREAM = FIXTURES / "omp-stream.jsonl"

OMP = {
    "launch": "cli",
    "command": "omp",
    "model": "local/deepseek-v4.1-flash",
}


def _lines(path: Path = STREAM) -> list[str]:
    return path.read_text().splitlines()


def _observe(lines: list[str] | None = None, **kwargs: object):
    return _backends.observe_stream(
        backend_name="probe",
        backend=OMP,
        lines=_lines() if lines is None else lines,
        **kwargs,
    )


def _plan(**kwargs: object):
    return _backends.launch_plan(
        backend_name="probe", backend=OMP, prompt="the brief", worktree="/wt", **kwargs
    )


# ── The dialect is registered and translates its command ────────────────────


def test_omp_is_a_known_dialect() -> None:
    assert "omp" in _backends.known_dialects()


def test_a_command_named_omp_selects_this_dialect() -> None:
    assert _plan().dialect == "omp"


# ── argv: the headless contract ─────────────────────────────────────────────


def test_the_prompt_rides_on_argv_and_no_stdin_text_is_fed() -> None:
    """A piped stdin is read as the prompt, and blocks until the pipe closes.

    The harness checks whether stdin is a terminal, and when it is not it reads
    the prompt from the pipe and waits for EOF before doing anything. A launch
    that feeds it text therefore stalls in the harness's own read phase, and a
    stream parser sees a worker that started and produced nothing — the exact
    silent failure this dialect is composed to avoid. The brief goes on the
    command line and the launch hands over no stdin text at all.
    """
    plan = _plan()

    assert plan.argv[-1] == "the brief"
    assert plan.stdin_text == ""


def test_the_run_uses_the_machine_readable_mode() -> None:
    plan = _plan(fence=False)
    assert plan.argv[:3] == ["omp", "-p", "--mode=json"]


def test_the_config_overlay_turns_off_every_worker_that_must_not_fan_out(
    tmp_path: Path,
) -> None:
    """The overlay named on the command line is read back and checked.

    Four of these keys turn off a capability and one disables a network tool
    pair. The project-MCP key is the one measured by a controlled experiment in
    the contract note: with it enabled a deliberately unloadable project server
    is discovered and fails, and with it disabled the same server is never
    discovered at all.
    """
    run = tmp_path / "run"
    run.mkdir()
    plan = _plan(manifest_path=str(run / "manifest.md"))

    overlay = next(word for word in plan.argv if word.startswith("--config="))
    declared = yaml.safe_load(Path(overlay.split("=", 1)[1]).read_text())

    assert declared["task"]["maxRecursionDepth"] == 0
    assert declared["browser"]["enabled"] is False
    assert declared["computer"]["enabled"] is False
    assert declared["web_search"]["enabled"] is False
    assert declared["fetch"]["enabled"] is False
    assert declared["mcp"]["enableProjectConfig"] is False


def test_the_tool_allowlist_names_only_what_a_worker_may_use() -> None:
    """`--tools` is the hard gate: an absent name is never offered.

    The overlay above is defence in depth against a renamed key; this is the
    gate. `task`, the browser, desktop control, the network tools and every
    MCP-provided tool are absent by construction, so no config change can
    present them.
    """
    plan = _plan()
    allowlist = next(word for word in plan.argv if word.startswith("--tools="))
    offered = set(allowlist.split("=", 1)[1].split(","))

    assert offered == {"read", "bash", "edit", "write", "grep", "glob", "todo"}
    for withheld in ("task", "browser", "computer", "web_search", "fetch"):
        assert withheld not in offered


def test_the_overlay_is_written_inside_the_run_the_launch_belongs_to(
    tmp_path: Path,
) -> None:
    run = tmp_path / "run"
    run.mkdir()
    plan = _plan(manifest_path=str(run / "manifest.md"))
    overlay = next(word for word in plan.argv if word.startswith("--config="))

    assert Path(overlay.split("=", 1)[1]).parent == run


def test_a_composition_with_no_run_writes_no_session_into_the_operator_home(
    tmp_path: Path,
) -> None:
    """A preview or a lane probe has no run home to keep a transcript in."""
    plan = _plan(manifest_path=str(tmp_path / "never-created" / "manifest.md"))
    assert "--no-session" in plan.argv
    overlay = next(word for word in plan.argv if word.startswith("--config="))
    assert Path(overlay.split("=", 1)[1]).is_file()


def test_a_live_run_keeps_its_session_so_it_can_be_resumed(tmp_path: Path) -> None:
    run = tmp_path / "run"
    run.mkdir()
    plan = _plan(manifest_path=str(run / "manifest.md"))
    assert "--no-session" not in plan.argv


def test_a_resume_names_the_session_and_keeps_it() -> None:
    plan = _plan(resume_session="01a1c6d3-39cd")
    assert plan.argv[plan.argv.index("--resume") + 1] == "01a1c6d3-39cd"
    assert "--no-session" not in plan.argv


def test_an_effort_outside_the_harness_vocabulary_is_not_invented() -> None:
    """An effort this harness cannot express leaves its own default in place."""
    plan = _backends.launch_plan(
        backend_name="probe",
        backend=dict(OMP, effort="medium"),
        prompt="p",
        worktree="/wt",
    )
    assert "--thinking" not in plan.argv

    declared = _backends.launch_plan(
        backend_name="probe",
        backend=dict(OMP, effort="high"),
        prompt="p",
        worktree="/wt",
    )
    assert declared.argv[declared.argv.index("--thinking") + 1] == "high"


# ── The run home ───────────────────────────────────────────────────────────


def test_the_run_home_is_relocated_through_home(tmp_path: Path) -> None:
    """`PI_CODING_AGENT_DIR` moves the agent store and leaves the logs behind.

    The harness resolves its root as the same directory its log directory hangs
    from, so a run that redirects only the store variable still appends a log
    file to the operator's `~/.omp/logs` — measured at one file per run. `HOME`
    moves the whole tree, which is why this entry names it.
    """
    declared = _worker_fence._HARNESS_HOME["omp"]
    assert declared[0] == "HOME"

    run = tmp_path / "run"
    run.mkdir()
    plan = _backends.launch_plan(
        backend_name="probe",
        backend=OMP,
        prompt="p",
        worktree=str(tmp_path),
        manifest_path=str(run / "manifest.md"),
        fence_home=str(tmp_path / "operator-home"),
    )

    assert plan.environment["HOME"] == str(run / "omp-home")
    assert (run / "omp-home").is_dir()


def test_a_dialect_with_no_declared_home_is_not_invented_one(tmp_path: Path) -> None:
    """The declared entry is what makes the home exist at all."""
    assert _worker_fence.harness_home("claude", tmp_path) is not None
    assert _worker_fence.harness_home("not-a-dialect", tmp_path) is None


# ── The stream parser ──────────────────────────────────────────────────────


def test_the_recorded_stream_reports_the_token_totals_it_recorded() -> None:
    """The run's own figures, not the all-zero placeholder beside them.

    This is the trap the contract note names: the first assistant message of
    every turn carries a usage record of all zeros, and the real totals arrive
    on the last. A parser that reads the first record reports this run as
    having spent nothing.
    """
    tokens = _observe().budget["tokens"]

    assert tokens["input_tokens"] == 32722
    assert tokens["output_tokens"] == 18
    assert tokens["cached_input_tokens"] == 0


def test_the_placeholder_cannot_replace_the_measured_totals() -> None:
    """Read the tail first, then the head: the totals must survive."""
    lines = _lines()
    head = [line for line in lines[:7] if line.startswith("{")]
    tail = lines[7:]

    extended = _observe(tail)
    merged = _observe(head, state=extended.stream_state)

    assert merged.budget["tokens"]["input_tokens"] == 32722
    assert merged.budget["tokens"]["output_tokens"] == 18


def test_the_session_id_tool_call_and_final_message_are_all_read() -> None:
    observation = _observe()

    assert observation.session_id == "01a1c6d3-39cd-7000-b58d-4ed2ecd74b85"
    assert observation.terminal is True
    assert observation.exit_status == "ok"
    assert observation.phase == "complete"
    assert (
        observation.final_message
        == "```\nhello from the probe fixture\nsecond line\n```"
    )
    # The stream carries one tool call, announced before the machine ran it.
    assert _backends._requests_tool(json.loads(_lines()[9])) is True


def test_prose_beside_the_json_does_not_stop_the_read() -> None:
    """The harness writes plain warnings onto the same stream as its records."""
    observation = _observe()
    assert any(line.startswith("Warning:") for line in _lines())
    assert observation.budget["tokens"]["input_tokens"] == 32722


def test_a_tool_call_is_machine_time_not_generation_time() -> None:
    """The machine's work is its own event on this stream, and is counted.

    A scan written only for the message-shaped dialects finds no tool call in an
    omp stream at all, which charges every second of tool wait to the model.
    """
    plan = _plan()
    assert _backends._requests_tool({"type": "tool_execution_start"}) is True
    assert _backends._requests_tool({"type": "tool_execution_end"}) is False
    assert plan.dialect == "omp"


# ── Positive control ──────────────────────────────────────────────────────


def test_the_token_reader_reports_non_zero_totals_for_the_healthy_stream() -> None:
    """Positive control: the instrument sees what is known to be present.

    The control is two-sided on purpose. The recorded healthy stream carries
    32,722 input and 18 output tokens and the reader must find them; a stream
    of the zero-usage placeholder alone must read as zero. One number proves
    the reading is real, the other proves the instrument can report zero at
    all — a summing routine that always returned a non-zero figure would pass
    the first check alone.
    """
    healthy = _observe()
    assert healthy.budget["tokens"]["input_tokens"] > 0
    assert healthy.budget["tokens"]["output_tokens"] > 0

    placeholder_only = _observe(
        [
            json.dumps(
                {
                    "type": "message_end",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "nothing yet"}],
                        "usage": {
                            "input": 0,
                            "output": 0,
                            "cacheRead": 0,
                            "cacheWrite": 0,
                            "totalTokens": 0,
                        },
                    },
                }
            ),
            json.dumps({"type": "agent_end", "messages": []}),
        ]
    )
    assert placeholder_only.budget["tokens"] is None


# ── Negative control ──────────────────────────────────────────────────────


def test_a_stream_in_another_dialects_vocabulary_is_refused_by_name() -> None:
    """A codex stream folded here would read as a healthy run that spent nothing.

    This is the declared negative control: remove the unrecognised-format
    refusal from the parser and this test fails, because the fold then returns
    a terminal-shaped observation with no tokens and no tool calls instead of
    raising.
    """
    stream = [
        json.dumps(
            {
                "type": "thread.started",
                "thread_id": "019ff509-8a60-7723-94fd-65942a6d8faa",
            }
        ),
        json.dumps(
            {
                "type": "turn.completed",
                "usage": {"input_tokens": 10, "output_tokens": 2},
            }
        ),
    ]

    with pytest.raises(_backends.BackendError) as excinfo:
        _observe(stream)

    assert "codex" in str(excinfo.value)
    assert "refused" in str(excinfo.value)
