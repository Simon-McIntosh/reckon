# ruff: noqa: I001, UP035
from __future__ import annotations
import json
import os
from pathlib import (
    Path,
)
from typing import (
    Any,
    Iterable,
    Mapping,
)



def _jsonl_events(path: Path) -> Iterable[Mapping[str, Any]]:
    """Yield readable objects from an append-only harness transcript."""
    try:
        lines = path.open(encoding="utf-8")
    except OSError:
        return
    with lines:
        for line in lines:
            try:
                event = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(event, Mapping):
                yield event


def _charged_claude_tokens(usage: Mapping[str, Any]) -> dict[str, int] | None:
    """Normalize one assistant request to the input total the meter charges."""

    def count(name: str) -> int:
        value = usage.get(name)
        measured = isinstance(value, (int, float)) and not isinstance(value, bool)
        return int(value) if measured else 0

    uncached = count("input_tokens")
    created = count("cache_creation_input_tokens")
    cached = count("cache_read_input_tokens")
    output = count("output_tokens")
    reasoning_detail = usage.get("output_tokens_details")
    reasoning = (
        int(reasoning_detail.get("thinking_tokens") or 0)
        if isinstance(reasoning_detail, Mapping)
        else 0
    )
    charged_input = uncached + created + cached
    if not charged_input and not output:
        return None
    return {
        "input_tokens": charged_input,
        "uncached_input_tokens": uncached,
        "cache_creation_input_tokens": created,
        "cached_input_tokens": cached,
        "output_tokens": output,
        "reasoning_output_tokens": reasoning,
        "total_tokens": charged_input + output,
    }


def _claude_authoring_turn(path: Path) -> dict[str, int] | None:
    """Return the latest complete assistant request from a Claude transcript."""
    latest = None
    for event in _jsonl_events(path):
        if event.get("type") != "assistant":
            continue
        message = event.get("message")
        usage = message.get("usage") if isinstance(message, Mapping) else None
        if isinstance(usage, Mapping):
            latest = _charged_claude_tokens(usage) or latest
    return latest


def _codex_authoring_turn(path: Path) -> dict[str, int] | None:
    """Return cumulative usage for the current Codex turn through dispatch."""
    latest = None
    for event in _jsonl_events(path):
        payload = event.get("payload")
        if not isinstance(payload, Mapping) or payload.get("type") != "token_count":
            continue
        info = payload.get("info")
        usage = info.get("total_token_usage") if isinstance(info, Mapping) else None
        if not isinstance(usage, Mapping):
            continue
        measured = {
            str(key): int(value)
            for key, value in usage.items()
            if isinstance(value, (int, float)) and not isinstance(value, bool)
        }
        latest = measured or latest
    return latest


def _ancestor_processes() -> tuple[set[int], list[str]]:
    """Return the bounded process ancestry used to identify the active harness."""
    pids: set[int] = set()
    names: list[str] = []
    pid = os.getppid()
    for _ in range(12):
        if pid <= 1 or pid in pids:
            break
        pids.add(pid)
        try:
            names.append((Path("/proc") / str(pid) / "comm").read_text().strip())
            fields = (Path("/proc") / str(pid) / "stat").read_text().split()
            pid = int(fields[3])
        except (OSError, IndexError, TypeError, ValueError):
            break
    return pids, names


def _latest_transcript(paths: Iterable[Path]) -> Path | None:
    candidates = []
    for path in paths:
        try:
            candidates.append((path.stat().st_mtime_ns, path))
        except OSError:
            continue
    return max(candidates, default=(0, None), key=lambda item: item[0])[1]


def _coordinator_runtime() -> tuple[str | None, str | None, Path | None]:
    """Resolve the calling harness, its session id, and current transcript."""
    ancestry, names = _ancestor_processes()
    claude_session = os.environ.get("CLAUDE_CODE_SESSION_ID", "").strip()
    codex_session = (
        os.environ.get("CODEX_SESSION_ID", "").strip()
        or os.environ.get("CODEX_THREAD_ID", "").strip()
    )
    try:
        claude_pid = int(os.environ.get("CLAUDE_PID", ""))
    except ValueError:
        claude_pid = -1
    harness = None
    runtime_session = None
    if claude_session and claude_pid in ancestry:
        harness, runtime_session = "claude-code", claude_session
    elif codex_session and any(name == "codex" for name in names):
        harness, runtime_session = "codex", codex_session
    elif claude_session and not codex_session:
        harness, runtime_session = "claude-code", claude_session
    elif codex_session:
        harness, runtime_session = "codex", codex_session
    elif claude_session:
        harness, runtime_session = "claude-code", claude_session
    if harness is None or runtime_session is None:
        return None, None, None

    user_home = Path.home()
    if harness == "claude-code":
        project_key = str(Path.cwd().resolve()).replace("/", "-")
        direct = (
            user_home
            / ".claude"
            / "projects"
            / project_key
            / f"{runtime_session}.jsonl"
        )
        search_root = user_home / ".claude" / "projects"
        transcript = (
            direct
            if direct.is_file()
            else _latest_transcript(search_root.glob(f"*/{runtime_session}.jsonl"))
        )
    else:
        search_root = user_home / ".codex" / "sessions"
        transcript = _latest_transcript(
            search_root.glob(f"*/*/*/*{runtime_session}.jsonl")
        )
    return harness, runtime_session, transcript


def _coordinator_accounting(session: str) -> dict[str, Any]:
    """Identify the dispatcher and measure the request that authored this run."""
    harness, runtime_session, transcript = _coordinator_runtime()
    tokens = None
    if transcript is not None and harness == "claude-code":
        tokens = _claude_authoring_turn(transcript)
    elif transcript is not None and harness == "codex":
        tokens = _codex_authoring_turn(transcript)
    authoring_turn = (
        {
            "status": "measured",
            "tokens": tokens,
            "source": f"{harness}-session-transcript",
        }
        if tokens is not None
        else {
            "status": "unknown",
            "tokens": None,
            "detail": "the dispatching harness exposed no authoring-turn token usage",
        }
    )
    return {
        "session_id": str(session),
        "runtime_session_id": runtime_session,
        "harness": harness,
        "authoring_turn": authoring_turn,
    }
