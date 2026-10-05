#!/usr/bin/env python3
"""Pre-tool-use hook: refuse a harness-native background-agent spawn where the
crew ledger already owns worker execution.

Self-contained and stdlib-only on purpose: this single file is distributed
into every crew-managed repository's harness settings, and most of those
repositories carry no dependency on the ``reckon`` package. It never imports
from ``reckon`` and never reads the host's registered-project mounts file —
only state local to the repository the spawn is happening in.

Behavior, in order:

1. Scope test — the repository is found by walking up from the session's
   working directory, so a session in any subdirectory is governed by it. A
   repository with no crew or flight state under ``docs/state/`` is untouched;
   the spawn is allowed silently.
2. Waiver — a live run pointer for this repository with an in-harness launch
   still awaiting attachment allows the spawn and names the bind command.
3. Waiver — an explicit environment override allows the spawn.
4. Otherwise the spawn is refused, and the refusal teaches the re-route so the
   caller can act in one step rather than diagnose the ban from a bare "no". A
   coordinator is told to do a lookup inline with its own tools and given the
   crew-dispatch invocation shape and watcher arming line for fan-out. A crew
   worker, identified by the run id dispatch exports into its environment, is
   told to do the reading inline within its own run.
"""

from __future__ import annotations

import json
import os
import shlex
import sys
from pathlib import Path
from typing import Any

# The harness tool that starts a new background agent. Matched by the harness
# hook wiring too (see the sync-owned hook config); checked again here so the
# script degrades safely if it is ever wired more broadly than intended.
GUARDED_TOOL = "Agent"

# Set for the session to bypass the guard for a spawn pattern it did not
# anticipate. Named in every refusal so the escape is one visible, deliberate
# step rather than a default.
OVERRIDE_ENV = "RECKON_ALLOW_NATIVE_AGENT"

# The run id crew dispatch exports into every worker it launches. Its presence
# marks the spawning session as a worker inside a ledgered run rather than a
# coordinator, which changes the re-route the refusal teaches.
RUN_ID_ENV = "RECKON_RUN_ID"

# The shape of the re-route a refusal teaches. Angle-bracket placeholders mark
# what only the plan and the caller's own work list can fill in; the guard
# has no plan context to resolve them from, only the repository's crew state.
DISPATCH_INVOCATION_SHAPE = (
    "reckon crew dispatch --project {project} --plan <slug> "
    "--section <section> --role {role} --node <node> "
    '--goal "<one deliverable>" --done-when "<measure>" '
    "--write-path <path> --time-budget <duration> --session <session>"
)


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


def repository_root(cwd: Path) -> Path:
    """The directory whose crew state governs a spawn made from ``cwd``.

    Walks up from ``cwd`` to the nearest directory carrying crew or flight
    state, so a session working in a subdirectory is governed by its
    repository. The walk stops at the first repository boundary, a directory
    holding ``.git`` (a file in a linked worktree), so a repository nested
    inside a crew-managed one is judged by its own state alone.
    """
    for candidate in (cwd, *cwd.parents):
        if crew_managed_projects(candidate) or (candidate / ".git").exists():
            return candidate
    return cwd


def _watch_arming_line(project: str) -> str:
    """The exact command that arms a watcher for a project's crew runs."""
    return f"reckon crew watch --project {shlex.quote(project)}"


def _awaiting_attach_pointer(repo_root: Path) -> dict[str, Any] | None:
    """The first live pointer for this repository awaiting in-harness attach."""
    live_dir = _config_home() / "crew" / "live"
    if not live_dir.is_dir():
        return None
    repo_key = str(repo_root)
    for path in sorted(live_dir.glob("*.json")):
        try:
            record = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(record, dict):
            continue
        if str(record.get("repo") or "") != repo_key:
            continue
        if record.get("launch") != "in-harness":
            continue
        if record.get("task"):
            continue
        return record
    return None


def _refusal_message(*, project: str) -> str:
    dispatch = DISPATCH_INVOCATION_SHAPE.format(project=project, role="investigate")
    watch = _watch_arming_line(project)
    return (
        "harness-native background agents are refused in this crew-managed "
        "repository: a native spawn bypasses the run ledger, the manifest "
        "contract, and calibration entirely. A lookup needs no agent: do it "
        "inline with your own Read, Grep, Bash or WebFetch. Route "
        "investigation or review fan-out through crew dispatch instead:\n"
        f"  {dispatch}\n"
        "Arm a watcher so the run's completion reaches this session:\n"
        f"  {watch}\n"
        "To waive this for a spawn pattern the guard did not anticipate, set "
        f"{OVERRIDE_ENV}=1 for this session."
    )


def _worker_refusal_message(*, run_id: str) -> str:
    return (
        "harness-native background agents are refused inside crew run "
        f"{run_id}: a nested agent gets no run record or manifest of its own. "
        "Do this reading inline with your own Read, Grep, Bash or WebFetch, "
        "within the run that is accountable for it."
    )


def decide(payload: dict[str, Any]) -> tuple[bool, str | None]:
    """Return ``(allowed, message)`` for one tool-call payload.

    ``message`` carries the refusal prose when ``allowed`` is False, and an
    informational note (the waiver in effect) on some allowed paths; it is
    None when there is nothing worth telling the caller.
    """
    if str(payload.get("tool_name") or "") != GUARDED_TOOL:
        return True, None

    cwd = str(payload.get("cwd") or os.getcwd())
    repo_root = repository_root(Path(cwd).resolve())
    projects = crew_managed_projects(repo_root)
    if not projects:
        return True, None

    pointer = _awaiting_attach_pointer(repo_root)
    if pointer is not None:
        directive = pointer.get("directive")
        attach = str(
            (directive or {}).get("attach_with")
            or f"reckon crew attach --run {pointer.get('run_id')} --task <task-id>"
        )
        return True, (
            "a crew-prepared in-harness run is awaiting this spawn; bind it "
            f"once it starts with `{attach}`"
        )

    if os.environ.get(OVERRIDE_ENV):
        return True, (
            f"{OVERRIDE_ENV} is set for this session; native spawn allowed "
            "by explicit override"
        )

    run_id = os.environ.get(RUN_ID_ENV)
    if run_id:
        return False, _worker_refusal_message(run_id=run_id)
    return False, _refusal_message(project=projects[0])


def hook_output(allowed: bool, message: str | None) -> dict[str, Any] | None:
    """Compose the harness pre-tool-use JSON for one decision.

    The harness parses this JSON only from stdout on exit 0. A refusal is a
    ``deny`` permission decision whose reason the calling session reads as the
    tool result. A note on an allowed spawn is addressed to that session, which
    must act on it, so it travels as ``additionalContext``, and also as
    ``systemMessage`` so the operator sees the waiver in effect.
    """
    if not allowed:
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": message,
            }
        }
    if not message:
        return None
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": message,
        },
        "systemMessage": message,
    }


def main() -> int:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except ValueError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    output = hook_output(*decide(payload))
    if output is not None:
        sys.stdout.write(json.dumps(output))
    return 0


if __name__ == "__main__":
    sys.exit(main())
