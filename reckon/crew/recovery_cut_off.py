"""Read an unfinished omp exit and the last tool its stream names."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any


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
    from reckon.crew.recovery_wait import _last_tool_call

    try:
        last_name, last_command = _last_tool_call(stream) or ("", "")
    except OSError:
        last_name, last_command = "", ""
    return {
        "attempt": exit_record.get("attempt") or record.get("attempt") or 1,
        "exited_at": exit_record.get("exited_at"),
        "last_tool": {"name": last_name, "arguments": {"command": last_command}},
    }


def cut_off_detail(cut_off: Mapping[str, Any]) -> str:
    tool = cut_off.get("last_tool") or {}
    name = tool.get("name") or "unrecorded tool"
    args = tool.get("arguments") or {}
    command = args.get("command")
    return f"{name}: {command}" if command else str(name)
