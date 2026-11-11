#!/usr/bin/env python3
"""Post-tool-use hook: hand the model the messages its coordinator wrote.

A headless worker cannot be interrupted, but the harness places a hook's
``hookSpecificOutput.additionalContext`` in front of the model before its next
step. This hook reads the run's own inbox directory, claims each message by
moving it out of the inbox, emits the claimed bodies as ``additionalContext``
and appends a receipt naming the message, the delivery time and the tool call it
followed.

Exactly-once delivery is the claim, not the read. A model that issues three
tool calls at once runs three hook processes at once; each lists the inbox
before any of them has claimed anything, so a hook that merely read a message
would deliver it three times. The claim is a rename into ``inbox/claimed/``,
which the kernel serialises: one process moves the file and the others find the
source gone and skip it. A rename that fails because the source has already been
moved is therefore the ordinary case, not an error.

The transport is the run directory the fleet already shares, so a message
survives a turn boundary, is auditable afterwards and reaches a resumed session.
Nothing here signals a process or writes to a descriptor.

Silent and cheap when there is nothing to say: no run directory, no inbox, or an
empty inbox writes no output at all and exits 0, because this runs after every
tool call of every worker carrying it.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

# The harness event this hook answers, and the tool whose payload names the run.
HOOK_EVENT = "PostToolUse"

# The run-directory layout. Messages sit flat in ``inbox`` and a delivered one
# is moved into ``claimed`` so a second hook process cannot read it again; the
# receipts are appended beside it, one JSON object per delivered message.
INBOX_DIRNAME = "inbox"
CLAIMED_DIRNAME = "claimed"
RECEIPTS_NAME = "receipts.jsonl"
MESSAGE_SUFFIX = ".md"

# The manifest field a worker writes to report what it received. A run that
# received a message quotes it; a run that received none says so. Named here,
# beside the reader that audits it, so the writer's contract and the check
# cannot drift.
MANIFEST_MESSAGES_KEY = "messages_received"
NO_MESSAGES_VALUE = "none"

CONTEXT_HEADER = (
    "Your coordinator wrote the following into your run's inbox. It was "
    "delivered at the tool call you just made, so act on it from here on:"
)


def _config_home() -> Path:
    """Resolve the reckon config home the way the reckon package does."""
    env = os.environ.get("RECKON_HOME")
    if env:
        return Path(env).expanduser()
    xdg = Path.home() / ".config" / "reckon"
    if xdg.exists():
        return xdg
    return Path.home() / "docs-server"


def _resolved(raw: Any) -> Path | None:
    """``raw`` as an absolute path, or None when it names nothing usable."""
    if not raw:
        return None
    try:
        return Path(str(raw)).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return None


def _live_records() -> Iterator[dict[str, Any]]:
    """Every readable live crew run record on this host, in path order."""
    live_dir = _config_home() / "crew" / "live"
    if not live_dir.is_dir():
        return
    for path in sorted(live_dir.glob("*.json")):
        try:
            record = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(record, dict):
            yield record


def run_directory(payload: dict[str, Any]) -> Path | None:
    """The run directory this tool call belongs to, or None.

    ``RECKON_MANIFEST`` is exported into every dispatched worker's environment
    and its parent is the run directory, so the ordinary answer costs one
    environment read. Without it the payload's working directory is matched
    against the live run pointers' worktrees, which is how a session that lost
    the variable still resolves its own run.

    Only a directory that exists is returned: a message written where no run
    lives would be delivered to nobody and read as delivered.
    """
    manifest = _resolved(os.environ.get("RECKON_MANIFEST"))
    if manifest is not None and manifest.parent.is_dir():
        return manifest.parent
    cwd = _resolved(payload.get("cwd") or os.getcwd())
    if cwd is None:
        return None
    for record in _live_records():
        if str(record.get("launcher_host") or "") != socket.gethostname():
            continue
        worktree = _resolved(record.get("worktree"))
        if worktree is None:
            continue
        if cwd != worktree and not cwd.is_relative_to(worktree):
            continue
        run = _resolved(record.get("manifest_path"))
        if run is not None and run.parent.is_dir():
            return run.parent
    return None


def message_name(now: float | None = None) -> str:
    """A message file name that sorts by write order and never collides."""
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime(now))
    micros = int(((now if now is not None else time.time()) % 1) * 1_000_000)
    return f"{stamp}{micros:06d}Z-{uuid.uuid4().hex[:8]}{MESSAGE_SUFFIX}"


def write_message(text: str, run_directory_path: Path) -> Path:
    """Write one message into a run's inbox, atomically and exclusively.

    The message is composed through the shared staged writer and renamed into
    place under a name no other writer can choose, so a reader never sees a
    partial message and two coordinators writing at once both land. Only the
    coordinator's writer reaches this function — the hook itself never writes —
    so the shared writer's import is paid once per message, not once per tool
    call.
    """
    from reckon._store import write_atomically

    inbox = Path(run_directory_path) / INBOX_DIRNAME
    inbox.mkdir(parents=True, exist_ok=True)
    destination = inbox / message_name()
    write_atomically(
        destination,
        lambda handle: handle.write(text),
        fsync_directory=True,
    )
    return destination


def pending_messages(run_directory_path: Path) -> list[Path]:
    """Every message waiting in a run's inbox, oldest first."""
    inbox = Path(run_directory_path) / INBOX_DIRNAME
    if not inbox.is_dir():
        return []
    return sorted(
        path
        for path in inbox.iterdir()
        if path.is_file() and path.name.endswith(MESSAGE_SUFFIX)
    )


def claim(path: Path) -> Path | None:
    """Take one message out of the inbox, or None if another hook took it.

    The rename is the claim. It is atomic, so of any number of hook processes
    racing on one message exactly one moves it and the rest are told the source
    is gone — which is the case this returns None for, rather than an error.
    """
    claimed = path.parent / CLAIMED_DIRNAME
    claimed.mkdir(parents=True, exist_ok=True)
    destination = claimed / path.name
    try:
        os.rename(path, destination)
    except FileNotFoundError:
        return None
    return destination


def append_receipt(
    run_directory_path: Path,
    *,
    name: str,
    payload: dict[str, Any],
    delivered_at: str | None = None,
) -> dict[str, Any]:
    """Append the one record that proves this message was delivered.

    Delivery is proved by this line and by nothing else: the file leaving the
    inbox shows a claim, while the receipt names the message, the moment it was
    handed to the model and the tool call it followed. It is appended with a
    single write to a file opened for append, so concurrent hooks cannot
    interleave one line with another.
    """
    record = {
        "message": name,
        "delivered_at": delivered_at
        or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "tool_name": payload.get("tool_name"),
        "tool_use_id": payload.get("tool_use_id"),
        "session_id": payload.get("session_id"),
    }
    inbox = Path(run_directory_path) / INBOX_DIRNAME
    inbox.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, sort_keys=True) + "\n"
    descriptor = os.open(
        inbox / RECEIPTS_NAME, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644
    )
    try:
        os.write(descriptor, line.encode())
    finally:
        os.close(descriptor)
    return record


def receipts(run_directory_path: Path) -> list[dict[str, Any]]:
    """Every delivery receipt recorded in a run directory, oldest first."""
    path = Path(run_directory_path) / INBOX_DIRNAME / RECEIPTS_NAME
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    records: list[dict[str, Any]] = []
    for line in lines:
        try:
            loaded = json.loads(line)
        except ValueError:
            continue
        if isinstance(loaded, dict):
            records.append(loaded)
    return records


def compose_context(delivered: list[tuple[str, str]]) -> str:
    """Render claimed messages as the context block the model reads."""
    blocks: list[str] = []
    for name, body in delivered:
        blocks.append(f"[{name}]\n{body.strip()}")
    return CONTEXT_HEADER + "\n\n" + "\n\n".join(blocks)


def deliver(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Claim and compose every waiting message, or None if there is nothing."""
    run = run_directory(payload)
    if run is None:
        return None
    delivered: list[tuple[str, str]] = []
    for path in pending_messages(run):
        claimed = claim(path)
        if claimed is None:
            continue
        try:
            body = claimed.read_text(encoding="utf-8")
        except OSError:
            continue
        delivered.append((claimed.name, body))
        append_receipt(run, name=claimed.name, payload=payload)
    if not delivered:
        return None
    return {
        "hookSpecificOutput": {
            "hookEventName": HOOK_EVENT,
            "additionalContext": compose_context(delivered),
        }
    }


def manifest_message_finding(text: str, run_directory_path: Path) -> str | None:
    """Report a manifest that disagrees with what its run directory records.

    A worker that received a message quotes it, and one that received none says
    so. The receipts are the run directory's own account of delivery, so they
    decide which of the two the worker owed: a receipt with no quoted message
    reads as a model that ignored its inbox, and no receipt with no statement
    reads as a channel nobody can tell from silence.
    """
    recorded = receipts(run_directory_path)
    if not recorded:
        if _manifest_says_none(text):
            return None
        return (
            f"{MANIFEST_MESSAGES_KEY}: no delivery receipt was recorded, so the "
            f"manifest must say `{MANIFEST_MESSAGES_KEY}: {NO_MESSAGES_VALUE}`"
        )
    for entry in recorded:
        name = str(entry.get("message") or "")
        body = _message_body(run_directory_path, name)
        if body and body.strip() and body.strip() in text:
            return None
    named = ", ".join(str(entry.get("message")) for entry in recorded[:3])
    return (
        f"{MANIFEST_MESSAGES_KEY}: {len(recorded)} delivery receipt(s) ({named}) "
        "record a message handed to this run, and the manifest quotes none of them"
    )


def _message_body(run_directory_path: Path, name: str) -> str:
    """The text of a delivered message, read back from the claimed inbox."""
    if not name:
        return ""
    path = Path(run_directory_path) / INBOX_DIRNAME / CLAIMED_DIRNAME / name
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _manifest_says_none(text: str) -> bool:
    """Whether a manifest carries the no-messages statement."""
    for line in text.splitlines():
        key, separator, value = line.partition(":")
        if not separator or key.strip() != MANIFEST_MESSAGES_KEY:
            continue
        if NO_MESSAGES_VALUE in value.strip().lower():
            return True
    return False


def main() -> int:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except ValueError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    try:
        output = deliver(payload)

    except Exception:  # noqa: BLE001 - a hook must never break the tool call
        return 0
    if output is not None:
        sys.stdout.write(json.dumps(output))
    return 0


if __name__ == "__main__":
    sys.exit(main())
