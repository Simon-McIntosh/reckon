"""A rebuilt backend keeps the window, model and effort, not just the command.

Reading a recorded run needs the command from the recorded argv and the
window, model and effort from the lane configuration or the row's own recorded
agent: whichever is dropped corrupts the reading. Dropping the window makes a
utilisation divide by whatever window the stream happened to announce — a
figure that overstates a large window by five times — and dropping model or
effort relaunches a resumed turn without the identity it ran as.

The fixture is deliberately stated with three models, because effort is the
positive control that proves the test is aimed: a lane without a model drops
model on every lane, so trusting the model assertion before the effort
assertion would pass a reconstruction that kept only the model.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reckon import _backends, crew
from reckon.crew.dispatch import _backend_settings
from reckon.crew.promotion import _terminal_stream_data

# The evidence row's peak prompt, divided against windows on both sides of the
# divisor the stream announces, so each direction of the defect is a number.
PEAK_INPUT = 129_784
ANNOUNCED_WINDOW = 200_000
# A configured window ABOVE the announced divisor: reading the announced one
# here overstates true utilisation by roughly five times.
CONFIGURED_WINDOW_ABOVE = 1_016_576
# A recorded window BELOW the announced divisor: reading the announced one here
# understates true utilisation by ten points.
RECORDED_WINDOW_BELOW = 172_800

CONFIG = {
    "backends": {
        "alpha": {
            "launch": "cli",
            "command": "clive",
            "model": "model-alpha",
            "effort": "high",
            "usable_input_window": CONFIGURED_WINDOW_ABOVE,
        },
        "beta": {
            "launch": "cli",
            "command": "clive",
            "model": "model-beta",
            "effort": "medium",
            "usable_input_window": RECORDED_WINDOW_BELOW,
        },
        # No window in the lane and none on the row: this backend's reading is
        # unknown until a window exists to divide by.
        "gamma": {
            "launch": "cli",
            "command": "clive",
            "model": "model-gamma",
            "effort": "low",
        },
    }
}

# The windows held open at assertion time, so a value that ever needs re-checking
# is derived from the fixture rather than frozen as a literal.
ANNOUNCED_PCT = round(100 * PEAK_INPUT / ANNOUNCED_WINDOW, 1)
TRUE_ABOVE_PCT = round(100 * PEAK_INPUT / CONFIGURED_WINDOW_ABOVE, 1)
TRUE_BELOW_PCT = round(100 * PEAK_INPUT / RECORDED_WINDOW_BELOW, 1)


@pytest.fixture()
def crew_home(tmp_path, monkeypatch):
    """Move every crew pointer into a temporary home."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home


def claude_events(context_window: int | None) -> list[str]:
    """One assistant request plus one completed result for a window reading."""
    assistant = {
        "type": "assistant",
        "session_id": "sess-window",
        "message": {
            "usage": {"input_tokens": PEAK_INPUT},
            "content": [{"type": "text", "text": "work"}],
        },
    }
    result = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "duration_ms": 10_000,
        "result": "done",
        "modelUsage": {
            "model-a": {
                "inputTokens": PEAK_INPUT,
                "outputTokens": 100,
                "cacheReadInputTokens": 0,
                "cacheCreationInputTokens": 0,
                "contextWindow": context_window,
            }
        },
        "usage": {"output_tokens": 100},
    }
    return [json.dumps(event) for event in (assistant, result)]


def _write_stream(directory: Path, context_window: int | None) -> Path:
    stream = directory / "stream.jsonl"
    stream.write_text("\n".join(claude_events(context_window=context_window)) + "\n")
    return stream


def _pointer(
    run_id: str,
    *,
    backend: str,
    model: str,
    effort: str,
    window: int | None,
    stream: Path | None = None,
) -> dict:
    record = {
        "run_id": run_id,
        "backend": backend,
        "launch": "cli",
        "argv": ["clive", "-p", "--output-format", "stream-json", "--verbose"],
        "agent": {
            "alias": "dsv4-flash",
            "backend": backend,
            "launch": "cli",
            "model": model,
            "effort": effort,
        },
    }
    if window is not None:
        record["agent"]["usable_input_window"] = window
    if stream is not None:
        record["log_path"] = str(stream)
    return record


def test_a_rebuilt_backend_divides_by_the_configured_window_not_the_announced_one() -> (
    None
):
    """The dispatch path keeps the window the lane enforces, dropping the announced one.

    These are the same truth with two denominators: 64.9% against the announced
    window, 12.8% against the configured lane window — five times overstate the
    run's real position against its own ceiling, and the reading must take the
    lane figure.
    """
    record = _pointer(
        "r-above",
        backend="alpha",
        model="model-alpha",
        effort="high",
        window=None,
    )
    settings = _backend_settings(record, CONFIG)

    # Nothing the rebuilt backend carries may be dropped: the config supplies
    # the window because the row recorded no usable_input_window of its own.
    assert settings["usable_input_window"] == CONFIGURED_WINDOW_ABOVE
    assert settings["model"] == "model-alpha"
    assert settings["effort"] == "high"

    throughput = _backends.observe_stream(
        backend_name="alpha",
        backend=settings,
        lines=claude_events(context_window=ANNOUNCED_WINDOW),
    ).throughput

    assert throughput["input_budget_tokens"] == CONFIGURED_WINDOW_ABOVE
    assert throughput["input_utilisation_pct"] == TRUE_ABOVE_PCT
    assert "configured lane" in throughput["detail"]
    assert f"window {CONFIGURED_WINDOW_ABOVE}" in throughput["detail"]
    # The direction excluded: utilising the announced divisor overstates by a
    # factor of about five, and no reading is allowed to land there.
    assert TRUE_ABOVE_PCT != ANNOUNCED_PCT
    assert TRUE_ABOVE_PCT * 5 < ANNOUNCED_PCT


def test_the_promotion_reading_uses_the_row_recorded_window(tmp_path) -> None:
    """The call that writes ``input_utilisation_pct`` divides by the row's own window.

    Promotion reads the terminal stream with the row's recorded settings, so
    the row that recorded a 172,800 window must report 75.1%, not the 64.9% a
    reader dividing by the 200,000 announced by the stream would record.
    """
    stream = _write_stream(tmp_path, context_window=ANNOUNCED_WINDOW)
    record = _pointer(
        "r-below",
        backend="beta",
        model="model-beta",
        effort="medium",
        window=RECORDED_WINDOW_BELOW,
        stream=stream,
    )
    measures = _terminal_stream_data(record)
    throughput = measures.throughput

    assert throughput["input_budget_tokens"] == RECORDED_WINDOW_BELOW
    assert throughput["input_utilisation_pct"] == TRUE_BELOW_PCT
    assert "configured lane" in throughput["detail"]
    assert TRUE_BELOW_PCT != ANNOUNCED_PCT
    assert TRUE_BELOW_PCT > ANNOUNCED_PCT


def test_an_unresolvable_window_records_unknown_never_a_substitute(tmp_path) -> None:
    """No window anywhere is unknown, and a substituted figure is refused.

    The gamma lane declares no window and the row recorded none, so the rebuilt
    backend has nothing to divide by. If it substituted any figure — the number
    the stream happens to announce, or a constant — the reading would record a
    plausible percentage and this assertion on ``None`` would fail.
    """
    stream = _write_stream(tmp_path, context_window=None)
    record = _pointer(
        "r-unknown",
        backend="gamma",
        model="model-gamma",
        effort="low",
        window=None,
        stream=stream,
    )
    measures = _terminal_stream_data(record)
    throughput = measures.throughput

    assert throughput["input_budget_tokens"] is None
    assert throughput["input_utilisation_pct"] is None
    assert "no window resolved" in throughput["detail"]


def test_no_reconstructed_caller_drops_a_setting() -> None:
    """Every reconstruction context keeps the window, model and effort.

    The rebuild has five callers — two observe a running row with configuration
    in hand, two read a promoted row without it, and the lane-change path reads
    a source harness — and all five share this one function. Before the merge
    every caller lost the window, model and effort; after it, a rebuild from
    recorded argv keeps the configured window and the row's own recorded model
    and effort, under configuration and without it, and a config-only rebuild
    keeps them from the lane mapping itself.
    """
    streamed = _pointer(
        "r-shared",
        backend="beta",
        model="model-beta",
        effort="medium",
        window=RECORDED_WINDOW_BELOW,
    )
    for settings in (
        _backend_settings(streamed, CONFIG),
        _backend_settings(streamed, None),
    ):
        assert settings["launch"] == "cli"
        assert settings["command"] == "clive"
        assert settings["model"] == "model-beta"
        assert settings["effort"] == "medium"
        assert settings["usable_input_window"] == RECORDED_WINDOW_BELOW

    unstreamed = {
        "run_id": "r-unstreamed",
        "backend": "alpha",
        "agent": {"backend": "alpha", "launch": "cli"},
    }
    settings = _backend_settings(unstreamed, CONFIG)
    assert settings["command"] == "clive"
    assert settings["usable_input_window"] == CONFIGURED_WINDOW_ABOVE
    assert settings["model"] == "model-alpha"
    assert settings["effort"] == "high"


def test_a_resumed_plan_carries_the_recorded_model_and_effort(crew_home) -> None:
    """The mapping a resumption hands the launch carries the row's model and effort.

    A resume recovers its backend from the recorded row, so the invocation it
    builds must relaunch with the same model and effort the run recorded. A
    reconstruction that returned only the command would relaunch with a default
    model and effort; this asserts both, and effort is the aimed control because
    a lane without a model drops model on every lane.
    """
    run_id = "r-resume-carries-identity"
    tree = crew_home / "trees" / run_id
    tree.mkdir(parents=True, exist_ok=True)
    ledger_root = crew_home / "ledger"
    ledger_root.mkdir(parents=True, exist_ok=True)
    crew._write_json(
        crew.pointer_path(run_id),
        {
            "run_id": run_id,
            "project": "proj",
            "repo": str(ledger_root),
            "worktree": str(tree),
            "launch": "cli",
            "backend": "beta",
            "sandbox": "worktree-full",
            "session_id": "sess-recorded-on-the-pointer",
            "argv": ["clive", "-p", "--output-format", "stream-json", "--verbose"],
            "agent": {
                "alias": "dsv4-flash",
                "backend": "beta",
                "launch": "cli",
                "model": "model-beta",
                "effort": "medium",
                "usable_input_window": RECORDED_WINDOW_BELOW,
            },
        },
    )

    plan = crew.resume_plan(run_id, "continue")

    assert plan.resumed_session == "sess-recorded-on-the-pointer"
    assert plan.argv[plan.argv.index("--model") + 1] == "model-beta"
    assert plan.argv[plan.argv.index("--effort") + 1] == "medium"


def test_a_resumption_states_it_was_not_context_checked(crew_home) -> None:
    """A resumed run says outright that its context fit was not re-verified.

    Only a fresh dispatch resolves context fit against the current repository; a
    resumption reuses the recorded session and never reaches that check. The
    pointer must carry that unchecked state explicitly — a reader who finds no
    marker could mistake an unchecked resumption for a verified one — and the
    marker must survive a genuine resumption, which merges rather than replaces
    the pointer.
    """
    run_id = "r-resume-says-unchecked"
    tree = crew_home / "trees" / run_id
    tree.mkdir(parents=True, exist_ok=True)
    ledger_root = crew_home / "ledger"
    ledger_root.mkdir(parents=True, exist_ok=True)
    directory = crew.run_dir(run_id)
    directory.mkdir(parents=True, exist_ok=True)
    resume_stream = directory / "resume-1.jsonl"
    resume_stream.write_text('{"type":"turn.started"}\n')
    manifest = directory / "manifest.md"
    manifest.write_text(f"node: {run_id}\nstatus: in-progress\n")
    crew._write_json(
        crew.pointer_path(run_id),
        {
            "run_id": run_id,
            "project": "proj",
            "repo": str(ledger_root),
            "worktree": str(tree),
            "launch": "cli",
            "backend": "beta",
            "sandbox": "worktree-full",
            "session_id": "sess-recorded-on-the-pointer",
            "manifest_path": str(manifest),
            "argv": ["clive", "-p", "--output-format", "stream-json", "--verbose"],
            "agent": {
                "alias": "dsv4-flash",
                "backend": "beta",
                "launch": "cli",
                "model": "model-beta",
                "effort": "medium",
                "usable_input_window": RECORDED_WINDOW_BELOW,
            },
        },
    )

    crew.resume_plan(run_id, "continue")

    stamped = crew.read_pointer(run_id)["context_fit"]
    assert stamped["checked"] is False
    assert stamped["state"] == "unchecked"
    assert stamped["window_tokens"] == RECORDED_WINDOW_BELOW
    assert "re-verifying context fit" in stamped["detail"]

    resumed = crew.record_resumption(
        run_id,
        pid=os.getpid(),
        turn=1,
        log_path=resume_stream,
        stderr_path=directory / "resume-1.stderr.log",
        attempt_started_at=datetime.now(tz=UTC).isoformat(),
        manifest_baseline_mtime_ns=manifest.stat().st_mtime_ns,
    )
    assert resumed["attempt"] == 2
    assert resumed["context_fit"]["checked"] is False
    assert resumed["context_fit"]["state"] == "unchecked"
