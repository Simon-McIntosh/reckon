#!/usr/bin/env python3
"""Pre-tool-use hook: refuse a peer send whose recipient resolves to a live
crew worker on this host.

Self-contained and stdlib-only on purpose: this single file is distributed
into every crew-managed repository's harness settings, and most of those
repositories carry no dependency on the ``reckon`` package. It never imports
from ``reckon`` and never reads the host's registered-project mounts file —
only state local to the repository the send is happening in and this host's
own crew run pointers.

Behavior, in order:

1. Scope test — a repository with no crew or flight state under
   ``docs/state/`` is untouched; the send is allowed silently.
2. Live-worker resolution — the recipient is matched by exact identity
   against this host's live run pointers (run id, node id, member, session,
   session id), never against a name pattern, so a coordinator session that
   happens to share a node's name is not caught. A pointer that did not
   record this host as its launcher is never matched.
3. Waiver — an explicit environment override allows the send.
4. Otherwise the send is refused, and the refusal names the resolved run id
   and the crew recovery-order command that reaches that run, so the caller
   steers the worker on a channel that delivers rather than diagnose a
   silent expiry.
"""

from __future__ import annotations

import json
import os
import shlex
import socket
import sys
from pathlib import Path
from typing import Any

# The harness tool that sends a message to a peer session. Matched by the
# harness hook wiring too (see the sync-owned hook config); checked again here
# so the script degrades safely if it is ever wired more broadly than intended.
GUARDED_TOOL = "SendMessage"

# Set for the session to bypass the guard for a send pattern it did not
# anticipate. Named in every refusal so the escape is one visible, deliberate
# step rather than a default.
OVERRIDE_ENV = "RECKON_ALLOW_PEER_MESSAGE"

# The shape of the re-route a refusal teaches: the recovery-order command that
# reaches a crew run whose turn has ended, carrying the message the send would
# have carried. The angle-bracket placeholder marks the message text the caller
# supplies; the run id is resolved from the matched pointer.
RESUME_INVOCATION_SHAPE = "reckon crew resume --run {run_id} --advice {advice}"


def _config_home() -> Path:
    """Resolve the reckon config home the same way the reckon package does.

    Duplicated rather than imported — see the module docstring.
    """
    env = os.environ.get("RECKON_HOME")
    if env:
        return Path(env).expanduser().resolve()
    xdg = Path.home() / ".config" / "reckon"
    if xdg.exists():
        return xdg
    return Path.home() / "docs-server"


def _this_host() -> str:
    """The launcher host liveness is keyed on (host-local run pointers only)."""
    return socket.gethostname()


def crew_managed_projects(repo_root: Path) -> list[str]:
    """Repository-local project names carrying crew or flight state.

    Reads only ``repo_root/docs/state/<project>/{crew.json,flight.yaml}`` —
    never the host's mounts file — so detection is correct for a repository
    nobody has registered on this host yet, and unaffected by what else is
    mounted here.
    """
    state_root = repo_root / "docs" / "state"
    if not state_root.is_dir():
        return []
    projects = []
    for entry in sorted(state_root.iterdir()):
        if not entry.is_dir():
            continue
        if (entry / "crew.json").is_file() or (entry / "flight.yaml").is_file():
            projects.append(entry.name)
    return projects


def _identity_values(record: dict[str, Any]) -> list[Any]:
    """Every field a send recipient could name this run by."""
    node = record.get("node")
    node_id = node.get("id") if isinstance(node, dict) else node
    return [
        record.get("run_id"),
        record.get("member"),
        record.get("session"),
        record.get("session_id"),
        node_id,
    ]


def _resolve_live_worker(recipient: str) -> dict[str, Any] | None:
    """The first live run pointer on this host whose identity names recipient."""
    live_dir = _config_home() / "crew" / "live"
    if not live_dir.is_dir():
        return None
    host = _this_host()
    for path in sorted(live_dir.glob("*.json")):
        try:
            record = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(record, dict):
            continue
        if str(record.get("launcher_host") or "") != host:
            continue
        if any(str(value) == recipient for value in _identity_values(record)):
            return record
    return None


def _refusal_message(*, recipient: str, pointer: dict[str, Any]) -> str:
    run_id = pointer.get("run_id") or "<run-id>"
    resume = RESUME_INVOCATION_SHAPE.format(
        run_id=shlex.quote(str(run_id)),
        advice='"<your message>"',
    )
    return (
        f'peer send refused: "{recipient}" resolves to live crew run '
        f"{run_id} on this host. A CLI-launched worker's session has no "
        "user, so a send to it returns success and is never delivered; "
        "steer this run on the channel that does work instead:\n"
        f"  {resume}\n"
        "Resume continues the same session with its prior context. A run "
        "still mid-turn resumes once its turn has ended. To waive this "
        f"send for a case the guard did not anticipate, set "
        f"{OVERRIDE_ENV}=1."
    )


def decide(payload: dict[str, Any]) -> tuple[bool, str | None]:
    """Return ``(allowed, message)`` for one tool-call payload.

    ``message`` carries the refusal prose when ``allowed`` is False, and an
    informational note (the waiver in effect) on the override path; it is None
    when there is nothing worth telling the caller.
    """
    if str(payload.get("tool_name") or "") != GUARDED_TOOL:
        return True, None

    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return True, None
    recipient = tool_input.get("to")
    if not isinstance(recipient, str) or not recipient.strip():
        return True, None

    cwd = str(payload.get("cwd") or os.getcwd())
    repo_root = Path(cwd).resolve()
    if not crew_managed_projects(repo_root):
        return True, None

    pointer = _resolve_live_worker(recipient.strip())
    if pointer is None:
        return True, None

    if os.environ.get(OVERRIDE_ENV):
        return True, (
            f"{OVERRIDE_ENV} is set for this session; peer send allowed "
            "by explicit override"
        )

    return False, _refusal_message(recipient=recipient.strip(), pointer=pointer)


def main() -> int:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except ValueError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    allowed, message = decide(payload)
    if allowed:
        if message:
            sys.stdout.write(json.dumps({"systemMessage": message}))
        return 0

    sys.stderr.write(
        json.dumps(
            {
                "hookSpecificOutput": {"permissionDecision": "deny"},
                "systemMessage": message,
            }
        )
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
