"""Launch translation, checked against event streams recorded from live runs.

Every observation test reads a fixture under ``tests/fixtures/backends/``, so the
whole matrix is verified with no process spawned and no network reached. The
fixtures were captured from real invocations; see the README beside them for what
was elided and why.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reckon import _backends

FIXTURES = Path(__file__).parent / "fixtures" / "backends"

CODEX = {"launch": "cli", "command": "codex", "sandbox": "worktree-full"}
CLAUDE = {"launch": "cli", "command": "claude", "sandbox": "worktree-full"}


def _lines(name: str) -> list[str]:
    return (FIXTURES / name).read_text().splitlines()


def _observe(backend: dict, name: str, backend_name: str = "b"):
    return _backends.observe_stream(
        backend_name=backend_name, backend=backend, lines=_lines(name)
    )


# ── Argument construction ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    "backend,expected",
    [
        (CODEX, "--dangerously-bypass-approvals-and-sandbox"),
        (CLAUDE, "--dangerously-skip-permissions"),
    ],
)
def test_worktree_full_runs_unsandboxed(backend, expected) -> None:
    """The worktree is the boundary, so the process itself is not sandboxed.

    A filesystem sandbox is inherited by child processes and breaks the test
    runners a worker's gate depends on, which is why the tier resolves to the
    harness's bypass flag rather than a sandbox.
    """
    plan = _backends.launch_plan(
        backend_name="b", backend=backend, prompt="p", worktree="/wt"
    )
    assert expected in plan.argv


@pytest.mark.parametrize(
    "backend,expected",
    [
        (dict(CODEX, sandbox="read-only"), ["--sandbox", "workspace-write"]),
        (dict(CLAUDE, sandbox="read-only"), ["--permission-mode", "plan"]),
    ],
)
def test_read_only_tier_withholds_writes(backend, expected) -> None:
    plan = _backends.launch_plan(
        backend_name="b",
        backend=backend,
        prompt="p",
        worktree="/wt",
        manifest_path="/delivery/manifest.md",
    )
    assert expected[0] in plan.argv
    assert plan.argv[plan.argv.index(expected[0]) + 1] == expected[1]


def test_read_only_codex_uses_the_delivery_directory_for_a_fresh_launch() -> None:
    plan = _backends.launch_plan(
        backend_name="b",
        backend=dict(CODEX, sandbox="read-only"),
        prompt="p",
        worktree="/wt",
        manifest_path="/delivery/manifest.md",
    )
    assert plan.argv == [
        "codex",
        "exec",
        "--json",
        "-C",
        "/delivery",
        "--sandbox",
        "workspace-write",
        "--skip-git-repo-check",
        "-",
    ]
    assert plan.cwd == "/delivery"


def test_read_only_codex_uses_the_delivery_directory_when_resumed() -> None:
    plan = _backends.launch_plan(
        backend_name="b",
        backend=dict(CODEX, sandbox="read-only"),
        prompt="advice",
        worktree="/wt",
        manifest_path="/redelivery/manifest.md",
        resume_session="S",
    )
    assert plan.argv == [
        "codex",
        "exec",
        "--json",
        "-C",
        "/redelivery",
        "--sandbox",
        "workspace-write",
        "--skip-git-repo-check",
        "resume",
        "S",
        "-",
    ]
    assert plan.cwd == "/redelivery"


@pytest.mark.parametrize(
    "sandbox,expected",
    [
        (
            "worktree-full",
            [
                "codex",
                "exec",
                "--json",
                "-C",
                "/wt",
                "--dangerously-bypass-approvals-and-sandbox",
                "-",
            ],
        ),
        (
            "workspace-write",
            [
                "codex",
                "exec",
                "--json",
                "-C",
                "/wt",
                "--sandbox",
                "workspace-write",
                "-",
            ],
        ),
    ],
)
def test_codex_write_tiers_keep_their_argv_and_worktree(sandbox, expected) -> None:
    plan = _backends.launch_plan(
        backend_name="b",
        backend=dict(CODEX, sandbox=sandbox),
        prompt="p",
        worktree="/wt",
        manifest_path="/delivery/manifest.md",
    )
    assert plan.argv == expected
    assert plan.cwd == "/wt"


def test_read_only_codex_requires_an_absolute_manifest_path() -> None:
    backend = dict(CODEX, sandbox="read-only")
    with pytest.raises(_backends.BackendError, match="absolute manifest path"):
        _backends.launch_plan(
            backend_name="b", backend=backend, prompt="p", worktree="/wt"
        )
    with pytest.raises(_backends.BackendError, match="absolute manifest path"):
        _backends.launch_plan(
            backend_name="b",
            backend=backend,
            prompt="p",
            worktree="/wt",
            manifest_path="manifest.md",
        )


def test_model_and_effort_reach_each_backend_in_its_own_form() -> None:
    codex = _backends.launch_plan(
        backend_name="b",
        backend=dict(CODEX, model="some-model", effort="high"),
        prompt="p",
        worktree="/wt",
    )
    claude = _backends.launch_plan(
        backend_name="b",
        backend=dict(CLAUDE, model="some-model", effort="high"),
        prompt="p",
        worktree="/wt",
    )
    assert ["-m", "some-model"] == codex.argv[
        codex.argv.index("-m") : codex.argv.index("-m") + 2
    ]
    assert "model_reasoning_effort=high" in codex.argv
    assert "--model" in claude.argv and "some-model" in claude.argv
    assert ["--effort", "high"] == claude.argv[
        claude.argv.index("--effort") : claude.argv.index("--effort") + 2
    ]


def test_absent_model_and_effort_emit_no_flags() -> None:
    """A backend that names no model must not gain an empty routing flag."""
    for backend in (CODEX, CLAUDE):
        plan = _backends.launch_plan(
            backend_name="b", backend=backend, prompt="p", worktree="/wt"
        )
        assert "-m" not in plan.argv
        assert "--model" not in plan.argv
        assert "--effort" not in plan.argv
        assert not any("model_reasoning_effort" in arg for arg in plan.argv)


def test_prompt_travels_on_stdin_for_every_backend() -> None:
    """A prompt passed as an argument can be swallowed by a variadic option."""
    for backend in (CODEX, CLAUDE):
        plan = _backends.launch_plan(
            backend_name="b", backend=backend, prompt="the prompt", worktree="/wt"
        )
        assert plan.stdin_text == "the prompt"
        assert "the prompt" not in plan.argv
    codex = _backends.launch_plan(
        backend_name="b", backend=CODEX, prompt="p", worktree="/wt"
    )
    assert codex.argv[-1] == "-"


def test_worktree_is_where_the_worker_runs() -> None:
    for backend in (CODEX, CLAUDE):
        plan = _backends.launch_plan(
            backend_name="b", backend=backend, prompt="p", worktree="/wt"
        )
        assert plan.cwd == "/wt"
        assert "/wt" in plan.argv


def test_resume_carries_the_session_id_in_each_dialect() -> None:
    codex = _backends.launch_plan(
        backend_name="b",
        backend=CODEX,
        prompt="advice",
        worktree="/wt",
        resume_session="S",
    )
    claude = _backends.launch_plan(
        backend_name="b",
        backend=CLAUDE,
        prompt="advice",
        worktree="/wt",
        resume_session="S",
    )
    assert codex.argv[codex.argv.index("resume") + 1] == "S"
    assert ["--resume", "S"] == claude.argv[
        claude.argv.index("--resume") : claude.argv.index("--resume") + 2
    ]
    assert codex.resumed_session == "S"


def test_resume_options_precede_the_subcommand_that_rejects_them() -> None:
    """Measured against the live harness: it refuses a working directory after
    `resume`, whose only arguments are a session id and a prompt."""
    plan = _backends.launch_plan(
        backend_name="b",
        backend=dict(CODEX, model="some-model", effort="high"),
        prompt="advice",
        worktree="/wt",
        final_message_path="/wt/final.txt",
        resume_session="S",
    )
    subcommand = plan.argv.index("resume")
    for option in (
        "-C",
        "-m",
        "-o",
        "-c",
        "--dangerously-bypass-approvals-and-sandbox",
    ):
        assert plan.argv.index(option) < subcommand, option
    assert plan.argv[subcommand + 1 :] == ["S", "-"]


def test_in_harness_backend_cannot_be_spawned() -> None:
    """Reckon cannot start the calling harness's own delegation primitive."""
    with pytest.raises(_backends.BackendError) as excinfo:
        _backends.launch_plan(
            backend_name="native",
            backend={"launch": "in-harness"},
            prompt="p",
            worktree="/wt",
        )
    assert "in-harness" in str(excinfo.value)


def test_unknown_command_says_what_can_be_translated() -> None:
    with pytest.raises(_backends.BackendError) as excinfo:
        _backends.dialect_for({"launch": "cli", "command": "some-other-harness"})
    message = str(excinfo.value)
    assert "some-other-harness" in message
    for dialect in _backends.known_dialects():
        assert dialect in message


def test_command_with_a_path_still_selects_its_dialect() -> None:
    plan = _backends.launch_plan(
        backend_name="b",
        backend=dict(CODEX, command="/usr/local/bin/codex"),
        prompt="p",
        worktree="/wt",
    )
    assert plan.dialect == "codex"


def test_missing_command_is_named_as_the_fault() -> None:
    with pytest.raises(_backends.BackendError) as excinfo:
        _backends.dialect_for({"launch": "cli"})
    assert "no command" in str(excinfo.value)


# ── Session capture, terminal events, final message ─────────────────────────


@pytest.mark.parametrize(
    "backend,fixture,session_id",
    [
        (CODEX, "codex-turn.jsonl", "019ff509-8a60-7723-94fd-65942a6d8faa"),
        (CLAUDE, "claude-turn.jsonl", "7c49fef5-2fc0-46c7-87ad-0c69347d6d6d"),
    ],
)
def test_session_id_is_captured_from_the_stream(backend, fixture, session_id) -> None:
    """A resumable id is what lets a worker's session outlive its workspace."""
    assert _observe(backend, fixture).session_id == session_id


@pytest.mark.parametrize(
    "backend,fixture", [(CODEX, "codex-turn.jsonl"), (CLAUDE, "claude-turn.jsonl")]
)
def test_completed_turn_reads_complete_with_its_message(backend, fixture) -> None:
    observation = _observe(backend, fixture)
    assert observation.terminal is True
    assert observation.exit_status == "ok"
    assert observation.phase == "complete"
    assert observation.final_message == "ready"


@pytest.mark.parametrize(
    "backend,fixture",
    [(CODEX, "codex-failed-turn.jsonl"), (CLAUDE, "claude-failed-turn.jsonl")],
)
def test_failed_turn_reads_failed_and_says_why(backend, fixture) -> None:
    """A failure must never read as a success, in either dialect.

    One harness labels a failed turn ``subtype: "success"`` beside its error
    flag, so a verdict taken from the label would be inverted.
    """
    observation = _observe(backend, fixture)
    assert observation.terminal is True
    assert observation.exit_status == "error"
    assert observation.phase == "failed"
    assert observation.detail
    assert observation.detail != "success"


def test_partial_stream_reads_as_working_not_finished() -> None:
    """Mid-run the log has no terminal event, and that is not a failure."""
    lines = _lines("codex-turn.jsonl")[:2]
    observation = _backends.observe_stream(backend_name="b", backend=CODEX, lines=lines)
    assert observation.phase == "working"
    assert observation.terminal is False
    assert observation.session_id


def test_half_written_line_is_counted_not_raised() -> None:
    """A JSON line still being written is normal while a worker runs."""
    lines = _lines("codex-turn.jsonl") + ['{"type": "turn.start']
    observation = _backends.observe_stream(backend_name="b", backend=CODEX, lines=lines)
    assert observation.malformed_lines == 1
    assert observation.phase == "complete"


def test_absent_log_reads_as_starting() -> None:
    observation = _backends.observe_log(
        backend_name="b", backend=CODEX, log_path="/nonexistent/stream.jsonl"
    )
    assert observation.phase == "starting"
    assert observation.budget["headroom"] == "unknown"


# ── Budget: asymmetric by design ────────────────────────────────────────────


def test_one_backend_reports_headroom_with_a_reset_time() -> None:
    budget = _observe(CLAUDE, "claude-turn.jsonl").budget
    assert budget["headroom"] == "known"
    assert budget["utilisation_pct"] == pytest.approx(1.02)
    assert budget["resets_at"] == "2026-09-01T00:00:00Z"
    assert budget["threshold_status"] == "allowed_warning"
    assert budget["rate_limit_type"] == "overage"
    assert budget["cost_usd"] is not None


def test_an_empty_rate_limit_mapping_is_not_a_measurement() -> None:
    budget = _backends.dialect_for(CLAUDE)._budget({})

    assert budget["headroom"] == "unknown"
    assert budget["utilisation_pct"] is None
    assert "no numeric utilisation" in budget["detail"]


@pytest.mark.parametrize("reset_epoch", [1_788_220_800_000, 1e100])
def test_invalid_reset_epoch_returns_a_null_reading(reset_epoch) -> None:
    lines = _lines("claude-turn.jsonl")
    event = json.loads(lines[2])
    event["rate_limit_info"]["resetsAt"] = reset_epoch
    lines[2] = json.dumps(event)

    observation = _backends.observe_stream(
        backend_name="b", backend=CLAUDE, lines=lines
    )

    assert observation.budget["resets_at"] is None


def test_the_other_backend_reports_tokens_and_no_headroom() -> None:
    """Token counts are not a budget, and the record must not imply they are."""
    budget = _observe(CODEX, "codex-turn.jsonl").budget
    assert budget["headroom"] == "unknown"
    assert budget["utilisation_pct"] is None
    assert budget["resets_at"] is None
    assert budget["tokens"]["input_tokens"] == 29253
    assert "no headroom" in budget["detail"]


def test_absence_of_a_signal_is_never_read_as_exhaustion() -> None:
    """The whole point of the unknown state: silence must not stop a wave."""
    for fixture, backend in (
        ("codex-turn.jsonl", CODEX),
        ("codex-failed-turn.jsonl", CODEX),
        ("claude-failed-turn.jsonl", CLAUDE),
    ):
        assert _backends.budget_exhausted(_observe(backend, fixture).budget) is None
    assert _backends.budget_exhausted(None) is None
    assert _backends.budget_exhausted({}) is None
    assert _backends.budget_exhausted({"headroom": "known"}) is None


def test_known_headroom_answers_the_exhaustion_question() -> None:
    assert (
        _backends.budget_exhausted(_observe(CLAUDE, "claude-turn.jsonl").budget)
        is False
    )
    spent = dict(_backends.unknown_budget(""), headroom="known", utilisation_pct=100.0)
    assert _backends.budget_exhausted(spent) is True


def test_observation_serialises_with_sorted_keys() -> None:
    data = _observe(CLAUDE, "claude-turn.jsonl").as_dict()
    assert list(data) == sorted(data)
    assert list(data["budget"]) == sorted(data["budget"])


def test_recorded_fixtures_cover_every_dialect() -> None:
    """Every distinct launch translation has a recorded event stream."""
    recorded = {path.name.split("-")[0] for path in FIXTURES.glob("*.jsonl")}
    translations = {
        _backends.dialect_for({"launch": "cli", "command": command}).name
        for command in _backends.known_dialects()
    }
    assert translations <= recorded
