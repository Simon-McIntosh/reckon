"""A codex run's worker time is measured from the rollout in its own run home.

The exec stream a codex run writes carries no timestamps, so its worker seconds
were recorded as the wall time on a normal finish and left unmeasured on a
stalled one. The client rollout already measures the model span, and promotion
already reads that receipt, but the locator searched only the client's session
home — the fixture here is copied in shape from
``r-20261016T055657532716-pick-state-render-slow-read``, whose rollout sits only
under its own run home's ``codex-home``. These tests pin the run-home search
and the receipt-derived figure: the fixture rollout sits only under a temporary
run home, the row records ``rollout_receipt`` with the receipt's span and both
figures in its throughput block, a run with no rollout keeps the wall fallback,
and a claude stream is unchanged.

Nothing here writes under the operator's ``~/.codex`` — the located rollout is
asserted to be the one under the temporary crew home.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reckon import _plan_html, crew
from reckon.crew import review as review_module
from reckon.crew import rollout
from reckon.crew.runs import _write_json, pointer_path

PROJECT = "proj"
PLAN = "plan-a"

# The rollout fixture's session and its measured span: two bounded tool calls
# (0->20 and 50->70) inside a 0->100 span, so machine_seconds is 40 and the
# application generation_seconds is 60.
CODEX_SESSION = "aaaa1111-bbbb-2222-cccc-333344445555"
CODEX_MACHINE_SECONDS = 40.0
CODEX_GENERATION_SECONDS = 60.0
CODEX_WORKER_SECONDS = int(CODEX_GENERATION_SECONDS + CODEX_MACHINE_SECONDS)


def _write_resource(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    bare = (
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{PROJECT}">'
        f"<title>{state['slug']}</title>"
        '</head><body><main class="plan-doc"></main></body></html>\n'
    )
    path.write_text(_plan_html.write_state(bare, state), encoding="utf-8")


def _seed_git_repository(root: Path) -> None:
    """Make the fixture a git worktree whose committed docs are the base.

    Promotion refuses a checkout that cannot host the landing commit, because
    the ledger row and the plan comment it writes must land in one commit.
    """
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "docs"),
        ("commit", "-q", "-m", "seed repository"),
    ):
        subprocess.run(["git", *arguments], cwd=root, check=True, capture_output=True)


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
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
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    _seed_git_repository(root)
    return root


def _stored_review(run_id: str) -> None:
    """Attach a complete independent review so the review gate is satisfied."""
    emitted = "\n".join(
        f"SCORE {dimension}: 20" for dimension in review_module.REVIEW_DIMENSIONS
    )
    record = review_module.parse_review(emitted)
    record.update(
        {
            "project": PROJECT,
            "reviewed_run_id": run_id,
            "review_run_id": f"review-of-{run_id}",
        }
    )
    review_module.store_review(record)


def _stamp(seconds_ago: int) -> str:
    moment = datetime.now(tz=UTC) - timedelta(seconds=seconds_ago)
    return moment.isoformat(timespec="seconds").replace("+00:00", "Z")


def _write_run_home_rollout(config_home: Path, run_id: str, start: datetime) -> Path:
    """A trimmed rollout carrying the codex session's own measured span.

    The record shape is copied from the run named in this module's docstring; the
    content is trimmed to the records the receipt reads. It is written only
    under the run's own harness home, which is what makes the run-home search
    the one that finds it.
    """

    def stamp(seconds: float) -> str:
        moment = start + timedelta(seconds=seconds)
        return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")

    records = [
        {"type": "session_meta", "payload": {"session_id": CODEX_SESSION}},
        {
            "type": "response_item",
            "payload": {"type": "custom_tool_call", "call_id": "call-1"},
        },
        {
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call_output",
                "call_id": "call-1",
            },
        },
        {
            "type": "response_item",
            "payload": {"type": "custom_tool_call", "call_id": "call-2"},
        },
        {
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call_output",
                "call_id": "call-2",
            },
        },
        {
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {
                        "input_tokens": 1200,
                        "cached_input_tokens": 400,
                        "output_tokens": 120,
                    },
                    "last_token_usage": {
                        "input_tokens": 1200,
                        "cached_input_tokens": 400,
                        "output_tokens": 120,
                    },
                    "model_context_window": 200_000,
                },
            },
        },
    ]
    offsets = (0.0, 0.0, 20.0, 50.0, 70.0, 100.0)
    lines = "".join(
        json.dumps({"timestamp": stamp(offset), "payload": record["payload"]}) + "\n"
        for record, offset in zip(records, offsets, strict=True)
    )
    path = (
        config_home
        / "crew"
        / "runs"
        / run_id
        / "codex-home"
        / "sessions"
        / "2026"
        / "10"
        / "16"
        / f"rollout-{start.strftime('%Y-%m-%dT%H-%M-%S')}-{CODEX_SESSION}.jsonl"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(lines, encoding="utf-8")
    return path


def _codex_status_lines(*, completed: bool) -> str:
    """A codex exec stream: no timestamps, an optional terminal turn."""
    lines = [{"type": "thread.started", "thread_id": CODEX_SESSION}]
    if completed:
        lines.append(
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 1200,
                    "cached_input_tokens": 400,
                    "output_tokens": 120,
                },
            }
        )
    return "".join(json.dumps(event) + "\n" for event in lines)


def _claude_status_lines() -> str:
    """A claude stream whose own timestamped events bound its span."""
    start = datetime.now(tz=UTC) - timedelta(seconds=5)
    first = start.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    last = (
        (start + timedelta(seconds=5))
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )
    return "".join(
        json.dumps(event) + "\n"
        for event in (
            {
                "type": "assistant",
                "session_id": "sess-claude",
                "timestamp": first,
                "message": {
                    "id": "m1",
                    "role": "assistant",
                    "type": "message",
                    "content": [{"type": "text", "text": "elided"}],
                    "usage": {
                        "input_tokens": 2,
                        "cache_read_input_tokens": 1000,
                        "cache_creation_input_tokens": 0,
                    },
                },
            },
            {
                "type": "result",
                "session_id": "sess-claude",
                "timestamp": last,
                "is_error": False,
                "result": "done",
                "duration_ms": 5000,
                "duration_api_ms": 3000,
                "num_turns": 1,
                "modelUsage": {
                    "claude-opus-5": {"outputTokens": 40, "contextWindow": 1000000}
                },
                "usage": {"input_tokens": 2, "output_tokens": 40},
            },
        )
    )


def _write_pointer(
    repository: Path,
    run_id: str,
    stream: Path,
    manifest: Path,
    *,
    backend: str,
    argv: list[str],
    session_id: str,
    time_budget: str,
) -> None:
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        "node: node-a\nstatus: complete\ncommits: none\n",
        encoding="utf-8",
    )
    stream.parent.mkdir(parents=True, exist_ok=True)
    _write_json(
        pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "repo": str(repository),
            "launch": "cli",
            "role": "implement",
            "member": "worker-a",
            "backend": backend,
            "argv": argv,
            "session_id": session_id,
            "created_at": _stamp(600),
            "manifest_path": str(manifest),
            "log_path": str(stream),
            "node": {
                "id": "node-a",
                "plan": PLAN,
                "section": "§2",
                "time_budget": time_budget,
                "write_paths": [],
            },
        },
    )


def _fast_settle(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shrink the settle's windows so the suite does not pay production timing."""
    from reckon.crew import promotion_checks

    monkeypatch.setattr(promotion_checks, "_STREAM_SETTLE_POLL_SECONDS", 0.01)
    monkeypatch.setattr(promotion_checks, "_STREAM_SETTLE_QUIESCENCE_SECONDS", 0.05)
    monkeypatch.setattr(promotion_checks, "_STREAM_SETTLE_MAX_SECONDS", 5.0)


def test_a_codex_receipt_measures_worker_seconds_on_a_normal_finish(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The measured defect: a codex row with a run-home rollout read wall_fallback.

    The rollout sits only under the run's own harness home, so the run-home
    search is what finds it. The row must then record the receipt's own span as
    its worker seconds rather than the wall time the exec stream forced.
    """
    run_id = "r-20261016T120000000000-codex-run"
    stream = tmp_path / "runs" / run_id / "stream.jsonl"
    manifest = tmp_path / "manifests" / f"{run_id}.md"
    _write_pointer(
        repository,
        run_id,
        stream,
        manifest,
        backend="codex",
        argv=["codex", "exec", "--json", "-"],
        session_id=CODEX_SESSION,
        time_budget="1h",
    )
    stream.write_text(_codex_status_lines(completed=True), encoding="utf-8")
    config_home = Path(str(tmp_path / "config"))
    located = _write_run_home_rollout(config_home, run_id, datetime.now(tz=UTC))

    # The run-home search is the one under test: the same session id resolves to
    # the fixture under the temporary crew home, not to anything under ~/.codex.
    assert rollout._locate_rollout(CODEX_SESSION) == located
    assert located.is_relative_to(config_home)

    monkeypatch.setattr("reckon.crew.promotion_checks.process_alive", lambda pid: False)
    _fast_settle(monkeypatch)
    _stored_review(run_id)

    promoted = crew.complete(
        run_id,
        gate="passed",
        outcome="the node recorded its outcome",
        root=repository,
    )
    run = promoted["record"]
    assert run["worker_seconds_source"] == "rollout_receipt"
    assert run["worker_seconds"] == CODEX_WORKER_SECONDS
    assert run["throughput"]["generation_seconds"] == CODEX_GENERATION_SECONDS
    assert run["throughput"]["machine_seconds"] == CODEX_MACHINE_SECONDS


def test_a_stalled_codex_receipt_still_measures_worker_seconds(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A killed codex run's rollout measures the span the stream never recorded.

    The stream reaches no terminal record, so completion falls to the mtime and
    the run is stalled against its budget; the receipt's span still measures it,
    and the throughput block carries both figures.
    """
    run_id = "r-20261016T120100000000-codex-stalled"
    stream = tmp_path / "runs" / run_id / "stream.jsonl"
    manifest = tmp_path / "manifests" / f"{run_id}.md"
    _write_pointer(
        repository,
        run_id,
        stream,
        manifest,
        backend="codex",
        argv=["codex", "exec", "--json", "-"],
        session_id=CODEX_SESSION,
        time_budget="5m",
    )
    stream.write_text(_codex_status_lines(completed=False), encoding="utf-8")
    _write_run_home_rollout(
        Path(str(tmp_path / "config")), run_id, datetime.now(tz=UTC)
    )

    monkeypatch.setattr("reckon.crew.promotion_checks.process_alive", lambda pid: False)
    _fast_settle(monkeypatch)
    _stored_review(run_id)

    promoted = crew.complete(
        run_id,
        gate="passed",
        outcome="the node recorded its outcome",
        root=repository,
    )
    run = promoted["record"]
    assert run["completed_at_source"] == "stream_mtime"
    assert run["worker_seconds_source"] == "rollout_receipt"
    assert run["worker_seconds"] == CODEX_WORKER_SECONDS
    assert run["throughput"]["generation_seconds"] == CODEX_GENERATION_SECONDS
    assert run["throughput"]["machine_seconds"] == CODEX_MACHINE_SECONDS


def test_a_codex_run_without_a_rollout_keeps_the_wall_fallback(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A codex run that wrote no rollout is measured exactly as before."""
    run_id = "r-20261016T120200000000-codex-no-rollout"
    stream = tmp_path / "runs" / run_id / "stream.jsonl"
    manifest = tmp_path / "manifests" / f"{run_id}.md"
    _write_pointer(
        repository,
        run_id,
        stream,
        manifest,
        backend="codex",
        argv=["codex", "exec", "--json", "-"],
        session_id=CODEX_SESSION,
        time_budget="1h",
    )
    stream.write_text(_codex_status_lines(completed=True), encoding="utf-8")

    monkeypatch.setattr("reckon.crew.promotion_checks.process_alive", lambda pid: False)
    _fast_settle(monkeypatch)
    _stored_review(run_id)

    promoted = crew.complete(
        run_id,
        gate="passed",
        outcome="the node recorded its outcome",
        root=repository,
    )
    run = promoted["record"]
    assert run["worker_seconds_source"] == "wall_fallback"
    assert run["worker_seconds"] == run["wall_seconds"]


def test_a_claude_stream_keeps_its_own_measured_figure(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A claude stream with its own timestamps is unchanged by the receipt path."""
    run_id = "r-20261016T120300000000-claude-run"
    stream = tmp_path / "runs" / run_id / "stream.jsonl"
    manifest = tmp_path / "manifests" / f"{run_id}.md"
    _write_pointer(
        repository,
        run_id,
        stream,
        manifest,
        backend="claude",
        argv=["claude", "-p"],
        session_id="sess-claude",
        time_budget="1h",
    )
    stream.write_text(_claude_status_lines(), encoding="utf-8")

    monkeypatch.setattr("reckon.crew.promotion_checks.process_alive", lambda pid: False)
    _fast_settle(monkeypatch)
    _stored_review(run_id)

    promoted = crew.complete(
        run_id,
        gate="passed",
        outcome="the node recorded its outcome",
        root=repository,
    )
    run = promoted["record"]
    assert run["worker_seconds_source"] == "stream_events"
    assert run["worker_seconds"] == 5
