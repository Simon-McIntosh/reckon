#!/usr/bin/env python3
"""Stop hook: refuse a crew worker's turn end while its run manifest is unfinished.

A Claude Code worker that ends its turn with a non-terminal manifest costs a
manual resume: the process ends, the record still says ``in-progress``, and a
coordinator cannot tell completion from a truncated turn. This hook makes the
manifest a precondition of the terminal stop.

The hook reads the harness's Stop payload on stdin. It resolves the run manifest
two ways, in order: from ``RECKON_MANIFEST`` (exported into every dispatched
worker's environment), else by matching the payload's working directory against
the live crew run pointers on this host. When no run resolves it writes nothing
and exits 0, so a coordinator or an interactive session is never affected.

When a run does resolve, the hook blocks in three cases: the manifest is absent,
its top-level ``status:`` line does not name a terminal value, or it is
terminal but was last written before this attempt began, which states that it was written
by an earlier attempt and not by the worker now stopping. A ``status: waiting``
is a fourth acceptable ending when the declaration beside it is one the fleet's
own reader honours — the worker has named what it is parked on and what ends
the park — and is refused, naming the missing field. The reason names the
manifest path and what is missing. So a worker that genuinely cannot finish is
never trapped, the hook blocks at most three times per stop chain, counting in a
file in the run directory. The chain is delimited by the payload's
``stop_hook_active`` flag: a fresh stop (``False``) resets the count, so a run
resumed after its predecessor exhausted the cap gets its own three refusals
rather than inheriting a spent counter. The stop allowed once the cap is reached
is not silent: the hook writes the terminal record itself, setting the manifest's
top-level status to ``blocked`` and appending the blocker line that names why, so
a capped run never reads as an ordinary stop.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

TERMINAL_STATUSES = frozenset({"complete", "blocked", "failed"})
WAITING_STATUS = "waiting"
BLOCK_LIMIT = 3
COUNTER_NAME = ".worker_stop_blocks"


def _config_home() -> Path:
    """Resolve the reckon config home the way the reckon package does."""
    env = os.environ.get("RECKON_HOME")
    if env:
        return Path(env).expanduser()
    xdg = Path.home() / ".config" / "reckon"
    if xdg.exists():
        return xdg
    return Path.home() / "docs-server"


def _manifest_from_pointer(cwd: Path) -> Path | None:
    """Manifest of the live crew run whose worktree holds ``cwd``.

    Matched when ``cwd`` is the worktree or any directory inside it, so a
    worker running in a subdirectory of its own worktree still resolves its run.
    """
    live_dir = _config_home() / "crew" / "live"
    if not live_dir.is_dir():
        return None
    for path in sorted(live_dir.glob("*.json")):
        try:
            record = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(record, dict):
            continue
        worktree = record.get("worktree")
        if not worktree:
            continue
        try:
            resolved = Path(str(worktree)).expanduser().resolve()
        except OSError:
            continue
        if cwd != resolved and not cwd.is_relative_to(resolved):
            continue
        manifest = record.get("manifest_path")
        if manifest:
            return Path(str(manifest)).expanduser()
    return None


def resolve_manifest(payload: dict[str, Any]) -> Path | None:
    """The run manifest for a Stop payload, or None when no run resolves."""
    env = os.environ.get("RECKON_MANIFEST")
    if env and env.strip():
        return Path(env).expanduser()
    cwd_raw = payload.get("cwd") or os.getcwd()
    try:
        cwd = Path(str(cwd_raw)).expanduser().resolve()
    except OSError:
        return None
    return _manifest_from_pointer(cwd)


def read_status(manifest: Path) -> str | None:
    """The manifest's top-level ``status:`` value, or None.

    Only a line starting at column zero counts. A ``status:`` line indented
    under another key is a nested value, not the manifest's own status.
    """
    if not manifest.is_file():
        return None
    try:
        text = manifest.read_text()
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("status:"):
            return line.split(":", 1)[1].strip() or None
    return None


def _manifest_predates_attempt(manifest: Path) -> bool:
    """Whether a manifest was last written before this worker attempt began."""
    raw = os.environ.get("RECKON_ATTEMPT_STARTED_AT", "").strip()
    if not raw or raw.endswith("z") or not manifest.is_file():
        return False
    if __package__ in (None, ""):
        # The registered hook command is this file's own path, so a bare-script
        # launch has not put the checkout that ships it on sys.path yet.
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from reckon._timestamps import parse_utc

    attempt_started_at = parse_utc(raw)
    if attempt_started_at is None:
        return False
    try:
        return manifest.stat().st_mtime_ns < int(
            attempt_started_at.timestamp() * 1_000_000_000
        )
    except (OSError, OverflowError):
        return False


def _wait_declaration(manifest: Path) -> tuple[bool, str]:
    """Whether the manifest's declared external wait is one the fleet acts on.

    Read through the classifier's own reader rather than a second copy of the
    wait grammar: a declaration the fleet would refuse as incomplete is not a
    state a turn may end on, and one reader means the hook and the classifier
    cannot disagree about what a parked worker is. Imported lazily, so an
    ordinary stop that declares no wait pays nothing for the validator.
    """
    if __package__ in (None, ""):
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    try:
        from reckon.crew.recovery import _manifest_wait
        from reckon.crew.reports import ManifestParseError, parse_manifest
    except ImportError as exc:
        return False, f"the wait reader could not be loaded ({exc})"
    try:
        data = parse_manifest(manifest.read_text(encoding="utf-8"))
    except (OSError, ManifestParseError, ValueError) as exc:
        return False, f"the manifest could not be read as a wait ({exc})"
    try:
        wait = _manifest_wait(
            data,
            manifest,
            now_seconds=time.time(),
            stale_after_seconds=0,
        )
    except Exception as exc:  # noqa: BLE001 - an unreadable wait is a refusal, not a crash
        return False, f"the wait declaration could not be validated ({exc})"
    if wait is None:
        return False, (
            "the wait fields name no actionable wait; wait_condition, "
            "wait_probe (or wait_file), wait_terminal and resume_brief must "
            "declare a condition that can end"
        )
    if not wait["valid"]:
        return False, str(wait.get("error") or "the wait declaration is incomplete")
    return True, ""


def _write_terminal_record(manifest: Path) -> None:
    """Set the manifest's top-level status to ``blocked`` with the reason.

    Preserves every other line the worker wrote. Creates the manifest when it is
    absent. Written atomically through a temp file in the same directory."""
    blocker = "blocker: turn ended without a terminal manifest after 3 refusals"
    try:
        text = manifest.read_text()
    except OSError:
        text = ""
    lines = text.splitlines()
    out: list[str] = []
    replaced = False
    for line in lines:
        if not replaced and line.startswith("status:"):
            out.append("status: blocked")
            replaced = True
        else:
            out.append(line)
    if not replaced:
        out.insert(0, "status: blocked")
    if blocker not in out:
        out.append(blocker)
    try:
        manifest.parent.mkdir(parents=True, exist_ok=True)
        tmp = manifest.parent / f".{manifest.name}.tmp"
        tmp.write_text("\n".join(out) + "\n")
        os.replace(tmp, manifest)
    except OSError:
        pass


def decide(payload: dict[str, Any]) -> tuple[bool, str | None]:
    """Return ``(blocked, reason)`` for one Stop payload.

    ``blocked`` is True when the stop is refused. When the stop is allowed it is
    False and ``reason`` is None, and the caller writes no output.
    """
    manifest = resolve_manifest(payload)
    if manifest is None:
        return False, None

    status = read_status(manifest)
    predates_attempt = _manifest_predates_attempt(manifest)
    if status in TERMINAL_STATUSES and not predates_attempt:
        return False, None
    # A declared wait is a finished turn: the worker has named what it is
    # parked on and what ends the park, so the record is complete and no resume
    # is owed until the condition lifts. Only a declaration the fleet's own
    # reader honours counts — a malformed one is refused with the missing field
    # named, so the next attempt can repair it rather than being forced to
    # write a status it cannot honestly claim.
    wait_refusal = ""
    if status == WAITING_STATUS and not predates_attempt:
        declared, wait_refusal = _wait_declaration(manifest)
        if declared:
            return False, None

    counter = manifest.parent / COUNTER_NAME
    if payload.get("stop_hook_active"):
        try:
            count = int(counter.read_text().strip() or "0")
        except (OSError, ValueError):
            count = 0
    else:
        count = 0
    if count >= BLOCK_LIMIT:
        _write_terminal_record(manifest)
        return False, None
    try:
        counter.parent.mkdir(parents=True, exist_ok=True)
        counter.write_text(f"{count + 1}\n")
    except OSError:
        pass

    if predates_attempt:
        what = "predates this attempt"
    elif status == WAITING_STATUS:
        what = f"declares status 'waiting' but {wait_refusal}"
    else:
        what = "is absent" if status is None else f"has status '{status}'"
    reason = (
        f"worker stop refused: the run manifest {manifest} {what}; it must be "
        "present with a status of complete, blocked or failed — or declare a "
        "waiting status with a well-formed wait block — before the turn can "
        f"end. Refusal {count + 1} of {BLOCK_LIMIT}."
    )
    return True, reason


def main() -> int:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except ValueError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    blocked, reason = decide(payload)
    if not blocked:
        return 0

    sys.stdout.write(json.dumps({"decision": "block", "reason": reason}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
