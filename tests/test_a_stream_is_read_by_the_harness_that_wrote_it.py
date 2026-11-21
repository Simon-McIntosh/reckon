"""A run's stream is read by the harness that wrote each attempt.

A run that changes lane keeps the streams its earlier harnesses wrote while its
pointer names the lane it carries now. Trusting the pointer reads those streams
through the wrong translation: the omp dialect refuses a stream written in
another vocabulary outright, so ``crew complete`` and ``crew observe`` died on
the run, and a dialect that does not refuse folds the stream to a
complete-looking run that spent nothing.

Each attempt is therefore folded by the harness whose own event vocabulary its
records open with, and the run's spend is the sum of every attempt's.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import _backends
from reckon import cli as cli_module
from reckon.crew import metering, promotion_checks, recovery_watch, runs
from tests.helpers.git import git as _git

PROJECT = "proj"
PLAN = "plan-a"
SESSION = "session-two-harness"

CLAUDE_INPUT = 100
CLAUDE_OUTPUT = 50
CODEX_INPUT = 200
CODEX_OUTPUT = 70

CLAUDE_STREAM = "".join(
    [
        json.dumps({"type": "system", "timestamp": "2026-11-20T10:00:00Z"}) + "\n",
        json.dumps(
            {
                "type": "assistant",
                "timestamp": "2026-11-20T10:00:05Z",
                "message": {
                    "id": "message-1",
                    "usage": {
                        "input_tokens": CLAUDE_INPUT,
                        "output_tokens": CLAUDE_OUTPUT,
                    },
                    "content": {
                        "type": "text",
                        "text": "first attempt",
                    },
                },
            }
        )
        + "\n",
        json.dumps(
            {
                "type": "result",
                "timestamp": "2026-11-20T10:00:10Z",
                "duration_ms": 10,
                "usage": {
                    "input_tokens": CLAUDE_INPUT,
                    "output_tokens": CLAUDE_OUTPUT,
                },
                "num_turns": 1,
            }
        )
        + "\n",
    ]
)

CODEX_STREAM = "".join(
    [
        json.dumps(
            {
                "type": "thread.started",
                "thread_id": "thread-1",
                "timestamp": "2026-11-20T10:10:00Z",
            }
        )
        + "\n",
        json.dumps(
            {
                "type": "turn.completed",
                "timestamp": "2026-11-20T10:10:05Z",
                "usage": {
                    "input_tokens": CODEX_INPUT,
                    "cached_input_tokens": 0,
                    "output_tokens": CODEX_OUTPUT,
                },
            }
        )
        + "\n",
    ]
)


def _write_two_harness_streams(home: Path, run_id: str) -> Path:
    """Write one attempt in each harness's vocabulary; return the newest."""
    directory = home / "crew" / "runs" / run_id
    directory.mkdir(parents=True)
    (directory / "stream.jsonl").write_text(CLAUDE_STREAM, encoding="utf-8")
    newest = directory / "lane-change-1.jsonl"
    newest.write_text(CODEX_STREAM, encoding="utf-8")
    return newest


@pytest.fixture()
def two_harness_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    """A live run whose two attempts another harness wrote."""
    home = tmp_path / "config-home"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    run_id = "r-two-harness"
    newest = _write_two_harness_streams(home, run_id)
    record = {
        "run_id": run_id,
        "project": PROJECT,
        "backend": "omp-local",
        "command": "omp",
        "dialect": "omp",
        "launch": "cli",
        "phase": "working",
        "log_path": str(newest),
        "stderr_path": str(newest.parent / "stderr.log"),
        "manifest_path": str(newest.parent / "manifest.md"),
        "node": {"id": run_id, "plan": PLAN, "time_budget": "20m"},
        "created_at": "2026-11-20T09:59:00Z",
    }
    (home / "crew" / "live").mkdir(parents=True)
    runs._write_json(runs.pointer_path(run_id), record)
    return {"home": home, "run_id": run_id, "record": record}


def test_each_attempt_is_folded_and_the_spend_of_both_is_summed(
    two_harness_run: dict,
) -> None:
    """The fold reaches both attempts, and the run's spend is both harnesses'."""
    record = two_harness_run["record"]

    measures = promotion_checks._terminal_stream_data(record)

    assert measures.completed_at is not None, measures
    assert "2026-11-20T10:10:05" in str(measures.completed_at), measures

    spend = metering.accumulate_run_spend(
        [record],
        two_harness_run["run_id"],
        streams_root=two_harness_run["home"] / "crew" / "runs",
    )
    assert spend.measured_stream_count == 2, spend
    assert spend.cumulative_input_tokens == CLAUDE_INPUT + CODEX_INPUT, spend
    assert spend.cumulative_output_tokens == CLAUDE_OUTPUT + CODEX_OUTPUT, spend


def test_crew_observe_returns_the_runs_state_without_a_dialect_refusal(
    two_harness_run: dict,
) -> None:
    import reckon.crew.dispatch  # noqa: F401 - the module, not the re-export

    module = sys.modules["reckon.crew.dispatch"]
    observed = module.observe(two_harness_run["run_id"])

    assert observed.get("phase") == "complete", observed.get("phase")
    assert observed["throughput"]["generated_tokens"] == CODEX_OUTPUT, observed


def test_the_live_view_lists_the_run_with_the_spend_of_both_harnesses(
    two_harness_run: dict,
) -> None:
    facts = recovery_watch._spend_facts(
        PROJECT,
        {"run_id": two_harness_run["run_id"]},
        spend_runs=[two_harness_run["record"]],
        rate_statuses={},
        streams_root=two_harness_run["home"] / "crew" / "runs",
    )

    assert facts["spend_measured_stream_count"] == 2, facts
    assert facts["spend_charged_tokens"] == (
        CLAUDE_INPUT + CODEX_INPUT + CLAUDE_OUTPUT + CODEX_OUTPUT
    ), facts


def test_a_run_written_by_one_harness_reads_as_it_did(tmp_path: Path) -> None:
    """A single-harness stream is read exactly as before this rule."""
    stream = tmp_path / "stream.jsonl"
    stream.write_text(CODEX_STREAM, encoding="utf-8")
    backend = {"command": "codex"}

    assert _backends.stream_writer_dialect(stream) == "codex"
    assert _backends._backend_for_stream(backend, stream) is backend
    observation = _backends.observe_log(
        backend_name="codex-lane",
        backend=backend,
        log_path=stream,
    )
    assert observation.terminal is True, observation
    assert observation.throughput["generated_tokens"] == CODEX_OUTPUT, observation

# ── the promotion path: a failed-gate run whose attempts two harnesses wrote ──

PROMOTION_CONFIG: dict = {
    "default_backend": "claude-lane",
    "local_backend": "claude-lane",
    "backends": {
        "claude-lane": {
            "launch": "cli",
            "command": "claude",
            "model": "m",
            "effort": "medium",
            "sandbox": "worktree-full",
            "time_budget": "20m",
        }
    },
    "roles": {"implement": {}, "review": {}},
    "fences": {"time_budget": "20m", "needs_help_after_failures": 2},
}

PLAN_HTML = (
    "<!DOCTYPE html>\n"
    "<head>\n"
    '<meta name="docs-project" content="proj">\n'
    '<meta name="reckon-type" content="plan">\n'
    '<meta name="plan-slug" content="plan-a">\n'
    '<meta name="plan-section-declarations" content="'
    "{&quot;open&quot;:&quot;implementable&quot;}"
    '">\n'
    "</head>\n"
    '<h2 id="open">An open task</h2>\n'
)


@pytest.fixture()
def promotion_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A synthesised repository carrying one implementable section."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    repository = tmp_path / "checkout"
    scripts = repository / "skills/reckon-build/scripts"
    scripts.mkdir(parents=True)
    source = Path(__file__).parents[1] / "skills/reckon-build/scripts/worktree_fleet.py"
    (scripts / "worktree_fleet.py").write_text(
        source.read_text(encoding="utf-8"), encoding="utf-8"
    )
    state = repository / "docs" / "state" / PROJECT
    state.mkdir(parents=True)
    (state / "index.json").write_text(
        json.dumps({"project": PROJECT, "data": {"_version": 0}}) + "\n",
        encoding="utf-8",
    )
    plans = repository / "docs" / "plans"
    plans.mkdir(parents=True)
    (plans / f"{PLAN}.html").write_text(PLAN_HTML, encoding="utf-8")


    _git(repository, "init", "-q", "-b", "main")
    _git(repository, "config", "user.email", "worker@example.invalid")
    _git(repository, "config", "user.name", "Worker")
    _git(repository, "add", "--", "docs")
    _git(repository, "commit", "-q", "-m", "chore: seed the checkout")
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repository / "docs")}), encoding="utf-8"
    )
    return repository


def _node(manifest: Path):
    from reckon import crew

    return crew.TaskNode(
        id="two-harness-node",
        plan=PLAN,
        section="open",
        goal="deliver a change a gate then fails",
        done_when="the gate passes at the delivered commit",
        write_paths=["delivered.txt"],
        manifest_path=str(manifest),
        spec_level="exact",
        role="implement",
        time_budget="20m",
    )


def _dispatched_run(repository: Path, tmp_path: Path) -> dict:
    """One implementation run dispatched with a stand-in launcher."""
    from reckon import crew

    manifest = tmp_path / "manifests" / "two-harness-node.md"
    base = _git(repository, "rev-parse", "HEAD")
    record = crew.dispatch(
        node=_node(manifest),
        project=PROJECT,
        repo=repository,
        config=PROMOTION_CONFIG,
        session=SESSION,
        base=base,
        check_budget=False,
        unreviewed_plan_override=True,
        launcher=lambda *_a, **_k: os.getpid(),
    )
    return {"run_id": str(record["run_id"]), "record": record, "manifest": manifest, "base": base}


def _deliver(run: dict) -> str:
    """Write the commit and the manifest the run's worker delivered."""
    worktree = Path(str(run["record"]["worktree"]))
    (worktree / "delivered.txt").write_text("the delivered change\n", encoding="utf-8")
    _git(worktree, "add", "--", "delivered.txt")
    _git(worktree, "commit", "-q", "-m", "test: deliver the change")
    head = _git(worktree, "rev-parse", "HEAD")
    run["manifest"].parent.mkdir(parents=True, exist_ok=True)
    run["manifest"].write_text(
        "\n".join(
            [
                "node: two-harness-node",
                "status: complete",
                "changed_paths:",
                "  - delivered.txt",
                "commits:",
                f"  - {head}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    run["head"] = head
    return head


def _give_another_harness_the_run(run: dict, home: Path) -> None:
    """Make the pointer name a lane whose dialect cannot read the streams."""
    directory = home / "crew" / "runs" / run["run_id"]
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "stream.jsonl").write_text(CLAUDE_STREAM, encoding="utf-8")

    def mutation(pointer: dict) -> dict:
        pointer["backend"] = "omp-local"
        pointer["command"] = "omp"
        pointer["dialect"] = "omp"
        pointer["log_path"] = str(directory / "lane-change-1.jsonl")
        pointer["pid"] = 999_999
        return pointer

    runs._mutate_pointer(run["run_id"], mutation)


def test_crew_complete_with_a_failed_gate_records_a_two_harness_run(
    promotion_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The command folds both attempts and writes the delivered row."""
    from reckon import crew_dispatch_commands
    from reckon.crew import open_run_index

    run = _dispatched_run(promotion_repo, tmp_path)
    head = _deliver(run)
    home = Path(os.environ["RECKON_HOME"])
    (home / "crew" / "runs" / run["run_id"] / "lane-change-1.jsonl").write_text(
        CODEX_STREAM, encoding="utf-8"
    )
    _give_another_harness_the_run(run, home)

    gate_log = tmp_path / "gate.log"
    gate_log.write_text("# command: false\nEXIT=1\n", encoding="utf-8")
    monkeypatch.setattr(
        crew_dispatch_commands, "_respawn_succeeds", lambda *_a, **_k: None, raising=False
    )
    result = CliRunner().invoke(
        cli_module.main,
        [
            "crew",
            "complete",
            "--wait",
            "--run",
            run["run_id"],
            "--gate",
            "failed",
            "--failure-classification",
            "negative-result",
            "--outcome",
            "the gate fails on the delivered change",
            "--commit",
            head,
            "--checkout-path",
            str(promotion_repo),
            "--gate-command",
            "false",
            "--gate-exit-status",
            "1",
            "--gate-log-path",
            str(gate_log),
        ],
    )

    assert result.exit_code == 0, result.output
    row = open_run_index.row_for_run(PROJECT, run["run_id"], promotion_repo)
    assert row is not None, result.output
    assert row["gate"] == "failed", row
    assert row["commits"] == [head], row
