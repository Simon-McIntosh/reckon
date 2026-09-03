from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable, Mapping

from reckon.crew.node import NEEDS_HELP_MARKER, TaskNode

# ── Prompt composition ──────────────────────────────────────────────────────


def compose_prompt(
    *,
    node: TaskNode,
    project: str,
    worktree: str,
    working_directory: str,
    manifest_path: str,
    time_budget: str,
    needs_help_after_failures: int,
    peer_scopes: Mapping[str, Iterable[str]] | None = None,
    run_id: str = "",
    peer_channels: Mapping[str, Mapping[str, str]] | None = None,
    peer_channel_path: str = "",
) -> str:
    """Compose a worker prompt from the four fences and a pointer to the plan.

    Deliberately short. Anything the live plan already says is omitted, because
    a copied brief drifts between workers and sessions while the plan does not.
    The worker's first act is to read the plan and section named here.
    """
    peers = peer_scopes or {}
    peer_lines = (
        "\n".join(
            f"  {name} → {', '.join(sorted(paths))}"
            for name, paths in sorted(peers.items())
        )
        or "  none"
    )
    channel_peers = peer_channels or {}
    channel_line = (
        ", ".join(
            f"{name}=run {details['run_id']}"
            for name, details in sorted(channel_peers.items())
        )
        or "none yet; later adjacent dispatches appear in peers.json"
    )
    peer_channel = Path(
        peer_channel_path or Path(manifest_path).parent / "peer-channel"
    )
    peer_client = (
        "python -c 'from reckon.crew.dispatch import _peer_command; "
        "raise SystemExit(_peer_command())'"
    )
    scope_lines = "\n".join(f"  {path}" for path in node.write_paths) or "  none"
    section = f" {node.section}" if node.section else ""
    specification_guidance = {
        "exact": (
            "SPEC     exact — implement as written and run the named check; "
            "deviation is a blocker to report.\n"
        ),
        "guided": (
            "SPEC     guided — the plan fixes the design; derive the implementation.\n"
        ),
        "open": (
            "SPEC     open — the plan fixes the goal and measure; design and implement.\n"
        ),
    }.get(node.spec_level, "")
    delivery_directory_note = ""
    if Path(working_directory) != Path(worktree):
        delivery_directory_note = f"""
RUNTIME FILESYSTEM
  The working directory is the delivery directory {working_directory}.
  The repository at the assigned worktree path {worktree} is read-only.
"""
    orientation_scope = json.dumps(list(node.write_paths), separators=(",", ":"))
    if node.role == "test":
        evidence_role_note = (
            " Your deliverable is an attribution, not a verdict: list new "
            "failures against the stated base separately from pre-existing "
            "ones, and name the candidate commit each new failure is "
            "attributed to."
        )
    else:
        evidence_role_note = (
            " This gate measures only this node's own change; verifying the "
            "merged result belongs to a separately dispatched test node, and "
            "a failure outside this node's declared scope is reported under "
            "follow_ons rather than triaged or fixed."
        )
    return f"""You are a worker on one node. Read the live plan first; it is the
semantic authority for context, decisions, evidence inputs and constraints.

NODE     {node.id}
GOAL     {node.goal}
PLAN     {project}:{node.plan}{section}
ROLE     {node.role}
{specification_guidance}{delivery_directory_note}

FENCE — SCOPE (exclusive write paths; nothing outside them)
{scope_lines}

CONCURRENT NODES (never touch their paths; request a scope change instead)
{peer_lines}

PEER CHANNEL — knowledge only; write scopes never transfer. Run {run_id}; endpoint {peer_channel}.
  Adjacent peers: {channel_line}
  Client prefix: {peer_client}
  List/ask operands: peer-list --run {run_id} OR peer-ask --run {run_id} --peer <run-or-node> --question "<question>"
  Read/reply operands: peer-read --run {run_id} --question-id <id> --wait <duration> OR peer-reply --run {run_id} --question-id <id> --answer "<answer>"
  Reads block on filesystem events; expiry writes NEEDS-HELP to the manifest.

FENCE — TIME
  {time_budget}. Exceeding it means stop and report, never push on.

FENCE — EVIDENCE (this measure is the done-when; state it quantitatively)
  {node.done_when}{evidence_role_note}

FENCE — DELIVERY
  Write your manifest to {manifest_path} BEFORE finishing, then reply with that path and a summary.
  If that exact path is not writable, STOP and report a blocker; a manifest written anywhere else means delivery cannot be found. Long output and logs go on disk.
MANIFEST (write exactly these keys; after reading the plan, observe path and revision in the assigned tree and make these first three lines your first write)
  orientation_worktree: <output of pwd>
  orientation_base_sha: <output of git rev-parse HEAD>
  orientation_write_paths: {orientation_scope}
  node: {node.id}
  status: complete | blocked | failed
  commits: <sha list>
  changed_paths: <explicit list>
  tests: <command and result>
  test_logs: <paths on disk>
  baseline_suite: <armed-only JSON: revision, command, exit_status, log_path or log_digest, completed, failure_count, failure_ids; completed=false is absent evidence>
  after_suite: <armed-only JSON: revision, command, exit_status, log_path or log_digest, completed, failure_count, failure_ids; completed=false is absent evidence>
  artifacts: <paths plus headline metrics>
  evidence_inputs: <facts the orchestrator needs for writeback>
  follow_ons: <work you found but were fenced out of, or none>
  blockers: <none, or the exact unmet condition>
WORKTREE AND PARALLEL-SAFETY RULES (binding)
  1. Work only in {worktree}. Do not create, checkout or switch branches.
  2. Never use git stash, rebase, clean, reset --hard, or path restoration.
  3. Stage explicit assigned paths only. Never git add -A/./*, commit -a/-am.
  4. Do not edit reckon plan or index state. Return outcome data instead.
  5. Commit locally with a conventional subject AND a body. Do not merge or
     push the primary branch.
  6. No AI attribution, and no plan, sprint or ticket identifiers in commit
     messages, symbol names, filenames or comments.
  7. Stop and report unexpected dirty files or unsafe scope.

IF YOU GET STUCK — stop and emit a report whose first line is
`{NEEDS_HELP_MARKER} <one line>` followed by all four of:
  tried:         what you attempted and the observable result
  options:       two or three concrete paths you can see
  leaning:       which one, and why
  cost-if-wrong: what must be redone if the wrong path is taken
Stop on any of: the same command failed {needs_help_after_failures} times with
different fixes attempted; a decision the plan does not settle is required;
the necessary change exceeds your write scope; the evidence cannot be produced
with the tools or data available; the time budget is spent with the measure
still unmet. Asking costs one turn; thrashing costs the node.
"""
