"""Read an unfinished omp exit and the last tool its stream names."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any


def _last_tool(path: Path) -> dict[str, Any]:
    """Search backwards in chunks so streamed output stays bounded in memory."""
    tool: dict[str, Any] = {}
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            position = handle.tell()
            pending = b""
            while position:
                size = min(position, 65536)
                position -= size
                handle.seek(position)
                lines = (handle.read(size) + pending).split(b"\n")
                pending = lines.pop(0) if position else b""
                for raw in reversed(lines):
                    try:
                        event = json.loads(raw)
                    except (ValueError, UnicodeDecodeError):
                        continue
                    if not isinstance(event, dict):
                        continue
                    if event.get("type") not in {
                        "tool_execution_start",
                        "tool_execution_update",
                        "tool_execution_end",
                    }:
                        continue
                    identity = event.get("toolCallId")
                    if tool and identity != tool.get("id"):
                        continue
                    tool = tool or {
                        "id": identity,
                        "name": event.get("toolName") or "unknown tool",
                    }
                    args = event.get("args")
                    if isinstance(args, dict):
                        tool["arguments"] = args
                        return tool
                    if event.get("type") == "tool_execution_start":
                        return tool
    except OSError:
        pass
    return tool


def omp_cut_off(
    record: Mapping[str, Any],
    *,
    manifest_status: str,
    exit_record: Mapping[str, Any] | None,
    stream: Path,
) -> dict[str, Any] | None:
    """Require an observed working exit and an unfinished manifest.

    Explicit waits and outcome words keep their own recovery rules. A process
    known live never supplies an exit record to this reader.
    """
    if manifest_status not in {"", "in-progress"} or exit_record is None:
        return None
    if exit_record.get("ended_during") != "working":
        return None
    words = [str(record.get("command") or ""), *record.get("argv", [])]
    if record.get("dialect") != "omp" and not any(
        Path(str(word)).name == "omp" for word in words
    ):
        return None
    return {
        "attempt": exit_record.get("attempt") or record.get("attempt") or 1,
        "exited_at": exit_record.get("exited_at"),
        "last_tool": _last_tool(stream),
    }


def cut_off_detail(cut_off: Mapping[str, Any]) -> str:
    tool = cut_off.get("last_tool") or {}
    name = tool.get("name") or "unrecorded tool"
    args = tool.get("arguments") or {}
    command = args.get("command")
    return f"{name}: {command}" if command else str(name)
