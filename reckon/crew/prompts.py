from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Iterable, Mapping

from reckon.crew.node import (
    NEEDS_HELP_MARKER,
    TaskNode,
    is_test_path,
    negative_control_is_none,
)

# The commit-and-manifest-early contract, embedded in every composed prompt.
# It lives here so it reaches a worker who never reads a reference document:
# the prompt embeds no protocol reference by design, so a discipline carried
# only by a reference file reaches nobody. Kept as a standalone constant so a
# test can compose with it masked out and diff against the live prompt, which
# proves the addition is removable and nothing else changed. The reason states
# loss recovery, never prevention: a durable write before a long generation
# survives that generation failing.
DURABLE_WRITE_CONTRACT = (
    "CONTRACT — DURABLE WRITES\n"
    "  Commit each deliverable as it completes rather than once at the end, and\n"
    "  write your manifest carrying whatever you already hold before beginning\n"
    "  any long output. This is recovery, not death prevention: a durable write\n"
    "  preceding a long generation survives that generation failing."
)

# The falsifiable-evidence contract, embedded in every composed prompt beside
# the durable-write one. It lives here for the same reason its sibling does:
# the prompt embeds no protocol reference by design, so a discipline carried
# only by a reference file reaches nobody. Every clause states a mechanical
# rule and names no repository, path, tool, model or project, because this text
# reaches workers on every project that syncs this package. Kept as a
# standalone constant so a test can compose with it masked out and diff against
# the live prompt, which proves the addition is removable and nothing else
# changed.
FALSIFIABLE_EVIDENCE_CONTRACT = (
    "CONTRACT — EVIDENCE THAT COULD HAVE FAILED\n"
    "  Confirm a previously recorded defect still reproduces before repairing it and\n"
    "  quote the reproduction; where the behaviour is already correct, verify it and\n"
    "  add the missing test rather than reimplementing a working guard.\n"
    "  A passing suite never shows that a guard fires, so make the guarded thing\n"
    "  happen and show the refusal.\n"
    "  Treat an implausible measurement as a claim about the instrument first: a\n"
    "  zero, an empty result or a uniform column needs the check shown to see\n"
    "  something known present before an absence is reported.\n"
    "  Read the receipt rather than the absence of an error, and verify a change\n"
    "  landed by a marker it introduced rather than one it removed.\n"
    "  Record the commits and paths you changed; a coordinator cannot see your\n"
    "  tree, so an unfilled field reads as no work."
)

# The worktree-landing contract, embedded only when the worker can write its
# assigned worktree, so a repository change is the deliverable it can actually
# commit. It lives here for the same reason its siblings do: the prompt embeds no
# protocol reference by design, so a discipline carried only by a reference file
# reaches nobody. The read-only tiers (review, investigate) operate in a delivery
# directory and receive no instruction to edit a repository they cannot write -
# the same condition that raises the RUNTIME FILESYSTEM note. Figure placement
# and the meta-line ban are stated because they bind the act of editing the plan
# in the tree. Kept as a standalone constant so a test can compose with it masked
# out and diff against the live prompt, which proves the addition is removable
# and scoped.
PLAN_LANDING_CONTRACT = (
    "CONTRACT — LANDING YOUR RECORD\n"
    "  Append your landing record to your own section of the plan and your evidence\n"
    "  anchor to the cumulative evidence record; both live in this worktree and both\n"
    "  go into your final commit.\n"
    "  Use a figure wherever a spatial, plotted or sequential relationship is clearer\n"
    "  shown than described, under docs/figures/<topic>/ with the project-absolute\n"
    "  src /<project>/figures/...; never an image of what is naturally a table.\n"
    "  Do not edit the plan-version or plan-modified meta lines: every worker\n"
    "  touching them makes every merge conflict there."
)

# The landing contract a brief dispatch receives, in place of the plan-target
# one. A brief names no committed plan section, so the plan-placement sentence
# would send the worker to a plan the run was never given; the run's own
# directory is the store its record belongs in. That directory is
# crew_home()/runs/<run_id> under the config home, which lies outside the
# worktree and is never committed, so this contract states where the record
# goes without telling the worker to commit it: a commit instruction naming a
# path outside the repository is refused by git, and a worker following it has
# no way to comply. The repository files the node changes are what it commits,
# and the manifest carries the record's absolute path so a reader can open it
# without knowing the run id. The header, the figure convention and the
# meta-line ban are unchanged, so the two landing contracts differ only in
# where the record goes and whether it is committed. Kept as a standalone
# constant so a test can compose with it masked out and diff against the live
# prompt, which proves the substitution is removable and scoped.
BRIEF_LANDING_CONTRACT = (
    "CONTRACT — LANDING YOUR RECORD\n"
    "  Write your landing record and your evidence anchor into this run's own\n"
    "  directory rather than into a plan section. That directory is the record's\n"
    "  home and lies outside the worktree, so it is never committed: commit only\n"
    "  the repository files your node changes, and name the record's absolute path\n"
    "  in your manifest so a later reader can open it without the run id.\n"
    "  Use a figure wherever a spatial, plotted or sequential relationship is clearer\n"
    "  shown than described, under docs/figures/<topic>/ with the project-absolute\n"
    "  src /<project>/figures/...; never an image of what is naturally a table.\n"
    "  Do not edit the plan-version or plan-modified meta lines: every worker\n"
    "  touching them makes every merge conflict there."
)

# The closure-authority contract, embedded only beside the landing contract, so a
# worker told how to land a record is also told what it may never close. It lives
# here for the same reason its siblings do: the prompt embeds no protocol reference
# by design, so a discipline carried only by a reference file reaches nobody. Both
# statements are about what the worker can see rather than about merge mechanics:
# only the coordinator observes the other nodes, so a worker knows its node landed
# but not whether the section closed. Kept as a standalone constant so a test can
# compose with it masked out and diff against the live prompt, which proves the
# addition is removable and scoped.
CLOSURE_AUTHORITY_CONTRACT = (
    "CONTRACT — WHO CLOSES THE NODE\n"
    "  A worker does not close its own node: it must not resolve its own\n"
    "  driving followup and must not set a terminal status, because only the\n"
    "  coordinator observes the other nodes — a worker knows its node landed,\n"
    "  not whether the section closed."
)

# The write-time manifest check, placed where the worker still holds the pen.
# The promotion gate reads the same file minutes to hours after the worker's
# process has ended, so a field it refuses can only be satisfied by a
# coordinator editing a record it did not author — and a repaired field and a
# fabricated one are the same bytes. A refusal that reaches the writer costs one
# edit inside the turn it is already having. The command carries the run's own
# id rather than naming the check bare, because an instruction to look for
# something is not an instruction to run it. The two fields are named because
# they refused four promotions on shape alone while the work was sound, and
# because the reader's status vocabulary is not guessable from the block alone.
# Kept as a standalone constant so a test can compose with it masked out and
# diff against the live prompt, proving the block is removable and scoped.
MANIFEST_CHECK_CONTRACT = (
    "CONTRACT — CHECK YOUR OWN RECORD BEFORE YOU CLOSE IT\n"
    "  Before you set status to a terminal value, run this and repair what it\n"
    "  reports — the write-time audit of your manifest against your own node\n"
    "  and worktree:\n"
    "  `{command}`\n"
    "  The same refusal at promotion reaches a coordinator hours later, after\n"
    "  you have ended, and a record repaired then is indistinguishable from a\n"
    "  fabricated one.\n"
    "  Two fields have refused promotions on shape alone while the work behind\n"
    "  them was sound. `status` must be one of the recognised values: a value\n"
    "  outside that vocabulary leaves the whole record unreadable and every\n"
    "  other field unread with it. `negative_control_log` must be the bare path\n"
    "  and nothing else on that line: an empty field, or one carrying a\n"
    "  description or a quoted first line, names nothing a reader can open."
)

# A node that writes a check declares the mutation that check must fail against,
# and promotion matches that declaration against the delivered red log's own text.
# The declaration was never shown to the worker, so the only available response
# was a paraphrase — refused by the same substring test the prompt gave it nothing
# to satisfy. The node's declaration is interpolated here beside the manifest
# requirement it answers, so the worker copies a string rather than inventing one.
# The declaration is rendered as written and never reflowed or re-indented: it is
# the exact string the log's first line has to repeat. A node that declares
# nothing keeps the subject and is told so, because an omitted subject reads as a
# requirement that does not apply.
NEGATIVE_CONTROL_DECLARATION_HEADER = (
    "CONTRACT — THE NEGATIVE CONTROL THIS NODE DECLARES\n"
)

NEGATIVE_CONTROL_DECLARED_RULE = (
    "  This node writes a check, so it declares the mutation that check must fail\n"
    "  against. The string your red log's first line must repeat, verbatim, is the\n"
    "  one below: promotion matches that exact string against the log's own text,\n"
    "  so a paraphrase, or a log that failed for any other reason, is refused.\n"
    "  Name that log's path in the `negative_control_log` line. Declared string:\n"
)

# The same declaration on a node whose scope reaches no test path. Promotion
# discharges a declared mutation only for a node that writes a check — it exempts
# a node whose write paths hold no test path with `node-writes-no-test-path`
# before it reads the declaration — so telling this worker it writes a check sends
# it to produce a red log nothing consumes. The branch is selected on the same
# predicate promotion applies, so the prompt states the property the node actually
# holds; the declaration is still rendered, because it is the node's record and a
# mutation the worker applies of its own accord belongs in the manifest.
NEGATIVE_CONTROL_DECLARED_NO_CHECK_RULE = (
    "  This node declares a mutation, but its write paths reach no test path, so\n"
    "  promotion reads no red log for it and no log is required. If you apply this\n"
    "  or another mutation to falsify a check you add, record it in the manifest\n"
    "  anyway. Declared string:\n"
)

NEGATIVE_CONTROL_NONE_RULE = (
    "  This node declares that no mutation applies. Such a declaration is expected\n"
    "  to carry its reason after `none:`; for a node that writes a check, promotion\n"
    "  refuses a bare one on a passing gate. No string is required for a red log's\n"
    "  first line to repeat, and no red log is required; the declaration stands\n"
    "  recorded for a later reader to judge.\n"
    "  Declared negative control:\n"
)

NEGATIVE_CONTROL_UNDECLARED = (
    "CONTRACT — THE NEGATIVE CONTROL THIS NODE DECLARES\n"
    "  None was declared on this node, so this dispatch states no string for a red\n"
    "  log's first line to repeat. If you apply a mutation of your own to falsify a\n"
    "  check you add, record it in the manifest anyway.\n"
)


def _negative_control_declaration(node: TaskNode) -> str:
    """Render this node's declared negative control where the worker can copy it.

    The declaration is interpolated as written and never reflowed or re-indented,
    because it is the string a delivered red log's first line has to repeat and
    promotion matches it against that log's text. A node that declares nothing is
    told so rather than having the subject dropped. A declared mutation is stated
    as a required red log only when the node's write paths reach a test path — the
    predicate promotion applies before it reads the declaration — so a node whose
    scope holds no test path is not told a property of its own node that is false.
    """
    declaration = str(node.negative_control or "").strip()
    if not declaration:
        return NEGATIVE_CONTROL_UNDECLARED
    if negative_control_is_none(declaration):
        rule = NEGATIVE_CONTROL_NONE_RULE
    elif any(is_test_path(path) for path in node.write_paths):
        rule = NEGATIVE_CONTROL_DECLARED_RULE
    else:
        rule = NEGATIVE_CONTROL_DECLARED_NO_CHECK_RULE
    return NEGATIVE_CONTROL_DECLARATION_HEADER + rule + "  " + declaration + "\n"


# This portion is deliberately constant for every worker, regardless of the
# project, node, role, or delivery configuration.  Its declared length is the
# boundary where per-node material may begin.
def _invariant_prompt_prefix() -> str:
    """Build the shared opening from the current contract constants."""
    return (
        "You are a worker on one node. Read the live plan first; it is the\n"
        "semantic authority for context, decisions, evidence inputs and constraints.\n\n"
        + DURABLE_WRITE_CONTRACT
        + "\n\n"
        + FALSIFIABLE_EVIDENCE_CONTRACT
        + "\n\nNODE     "
    )


INVARIANT_PROMPT_PREFIX = _invariant_prompt_prefix()
INVARIANT_PROMPT_BOUNDARY = len(INVARIANT_PROMPT_PREFIX)

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
    can_write_worktree: bool | None = None,
    host_line: str = "",
    brief: str = "",
) -> str:
    """Compose a worker prompt from the four fences and its task authority.

    Deliberately short. Anything the live plan already says is omitted, because
    a copied brief drifts between workers and sessions while the plan does not.
    The worker's first act is to read the plan and section named here, or, when
    a brief is supplied, the brief carried in its place: a brief dispatch names
    no committed plan section, so the plan pointer is replaced by the brief text
    and the landing contract sends the record to the run directory rather than
    to a plan section. A brief is carried verbatim, so a later reader can diff
    the brief's own bytes against the prompt that read them.
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
    # The client names the interpreter the composing process runs under, not a
    # bare name the worker's PATH must resolve: `python -c` puts only the cwd
    # on the import path, so a bare name works solely for a worker standing in
    # the package source tree and fails for the report roles whose delivery
    # directory is elsewhere. The composing interpreter can import the package
    # from any directory, which is the property being relied on; it is derived
    # at composition time, never written as a literal path.
    peer_client = (
        f"{sys.executable} -c 'from reckon.crew.dispatch import _peer_command; "
        "raise SystemExit(_peer_command())'"
    )
    scope_lines = "\n".join(f"  {path}" for path in node.write_paths) or "  none"
    section = f" {node.section}" if node.section else ""
    # The plan pointer is the plan-section read instruction, and the plan name
    # and section it names are the whole of it. A brief replaces that block in
    # place and verbatim, so the brief prompt carries no plan pointer; every
    # other part of the contract is composed for both carriers.
    task_authority = (
        f"BRIEF\n{brief}" if brief else f"PLAN     {project}:{node.plan}{section}"
    )
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
    host_context = f"{host_line}\n" if host_line else ""
    delivery_directory_note = ""
    if Path(working_directory) != Path(worktree):
        delivery_directory_note = f"""
RUNTIME FILESYSTEM
  The working directory is the delivery directory {working_directory}.
  The repository at the assigned worktree path {worktree} is read-only.
"""
    # The landing contract is a named slot in the template like its siblings,
    # gated to the shape where a repository change is the deliverable. The gate
    # is whether the worker can write its assigned worktree — the fact that
    # decides if a change is something it can commit — resolved by dispatch and
    # never by which dialect happens to relocate the process directory. Each
    # slot is separated by blank lines so a test can mask any one constant and
    # recompose: the result must equal the live prompt with that block deleted.
    if can_write_worktree is None:
        can_land = Path(working_directory) == Path(worktree)
    else:
        can_land = can_write_worktree
    if not can_land:
        landing_contract = ""
    else:
        landing_contract = BRIEF_LANDING_CONTRACT if brief else PLAN_LANDING_CONTRACT
    closure_authority_contract = CLOSURE_AUTHORITY_CONTRACT if can_land else ""
    # The check is composed with this run's own id so the worker can execute the
    # line as written. A composing caller that supplies no id still gets a
    # runnable instruction rather than a command that silently checks nothing.
    manifest_check_contract = MANIFEST_CHECK_CONTRACT.format(
        command=(
            f"reckon crew check-manifest --run {run_id}"
            if run_id
            else "reckon crew check-manifest --run <this run's id>"
        )
    )
    negative_control_declaration = _negative_control_declaration(node)
    orientation_scope = json.dumps(list(node.write_paths), separators=(",", ":"))
    if node.role == "test":
        evidence_role_note = (
            " Your deliverable is an attribution, not a verdict: list new "
            "failures against the stated base separately from pre-existing "
            "ones, and name the candidate commit each new failure is "
            "attributed to. Record the pre-existing set in baseline_suite, "
            "the merged-head result in after_suite, and every added failure "
            "in failure_attribution. The repository is read-only for this "
            "role; write only the manifest, report, and logs under the "
            "declared delivery scope."
        )
    else:
        evidence_role_note = (
            " This gate measures only this node's own change; verifying the "
            "merged result belongs to a separately dispatched test node, and "
            "a failure outside this node's declared scope is reported under "
            "follow_ons rather than triaged or fixed."
        )
    return f"""{_invariant_prompt_prefix()}{node.id}
GOAL     {node.goal}
{task_authority}
ROLE     {node.role}
{specification_guidance}{host_context}{delivery_directory_note}

{landing_contract}

{closure_authority_contract}

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
  {time_budget}. Exceeding it means stop and report, never push on. Your process ends when this turn ends: never wait across a backgrounded command. Write your manifest with what you know now before starting one, and update it afterward if a later turn arrives — that is keyed to starting the wait, not to finishing the work. A run that ends mid-wait is resumable, so if you run out of turns, leave a record naming exactly what you were waiting for — set status: waiting and fill the wait_condition, wait_probe, wait_terminal and resume_brief keys in the manifest block so a resume sweep can wake you. Declaration is for a wait you hold: the wait block states something you are actually waiting on, never a note about where you are. A resumed worker whose wait is met has nothing left to wait on: record where work stands under the checkpoint key and continue, rather than writing the wait block again. So does any worker recording progress at any point, not only a resumed one.

FENCE — EVIDENCE (this measure is the done-when; state it quantitatively)
  {node.done_when}{evidence_role_note}

FENCE — DELIVERY
  Write your manifest to {manifest_path} BEFORE finishing, then reply with that path and a summary.
  If that exact path is not writable, STOP and report a blocker; a manifest written anywhere else means delivery cannot be found. Long output and logs go on disk.

{manifest_check_contract}

{negative_control_declaration}
MANIFEST (write exactly these keys; after reading the plan, observe path and revision in the assigned tree and make these first three lines your first write; those three lines are the orientation write — record them under status: in-progress with a checkpoint line, and leave the wait fields empty until you are actually waiting on an external condition)
  orientation_worktree: <output of pwd>
  orientation_base_sha: <output of git rev-parse HEAD>
  orientation_write_paths: {orientation_scope}
  node: {node.id}
  status: in-progress | waiting | complete | blocked | failed
  wait_condition: <only when an external condition is actually awaited — never at the orientation write: one line stating what is being waited on, and what must happen for the wait to end>
  wait_probe: <only when an external condition is actually awaited — never at the orientation write: the shell-free argument vector that answers the condition, run in your worktree; for example ["squeue","-h","-j","1271081"]>
  wait_terminal: <only when an external condition is actually awaited — never at the orientation write: the probe output values that mean the wait is over; they are matched against the probe's last line, with exit:<code> standing in when it prints nothing; for example exit:0>
  resume_brief: <only when an external condition is actually awaited — never at the orientation write: what the resumed self does next>
  checkpoint: <when you are recording progress and not setting status to waiting — any worker recording progress at any point, not only a resumed worker whose wait is met: one line recording where work stands and what comes next, so you leave a checkpoint rather than a wait block>
  commits: <sha list>
  changed_paths: <explicit list>
  tests: <the gate command that actually ran, and its result — never a template command carrying an unsubstituted placeholder>
  test_logs: <paths on disk>. A gate log's first line names the revision it ran at, the tree, and the command; a gate or base-arm measurement run in a scratch tree also names on its header lines the absolute path of the module under test as imported (`module.__file__`) and the resolved working directory the run resolved from
  measurement_module: <only for a gate or base-arm measurement run in a scratch tree: the absolute path of the module under test as imported — the value the run printed for `module.__file__`>
  measurement_cwd: <only for a gate or base-arm measurement run in a scratch tree: the resolved working directory the run resolved from>
  negative_control_log: <the path alone, and nothing else on this line — no description, note or continuation>. Required when the node's write paths include a test file and its negative_control is not `none: <reason>`. The log's first line repeats the declared mutation verbatim, so a log that failed for any other reason is refused
  negative_control_note: <where an explanation goes: one line of commentary on the red log named above, since that value stands alone; omit when the log is self-explanatory>
  baseline_suite: <armed-only JSON: revision, command, exit_status, log_path or log_digest, completed, failure_count, failure_ids; completed=false is absent evidence>
  after_suite: <armed-only JSON: revision, command, exit_status, log_path or log_digest, completed, failure_count, failure_ids; completed=false is absent evidence>
  failure_attribution: <armed-only, test role JSON {{failure_id: candidate_commit}} for each newly added failure>
  artifacts: <paths plus headline metrics>
  evidence_inputs: <facts the orchestrator needs for writeback>
  follow_ons: <work you found but were fenced out of, or none>
  blockers: <none, or the exact unmet condition>
WORKTREE AND PARALLEL-SAFETY RULES (binding)
  1. Work only in {worktree}. Do not create, checkout or switch branches.
  2. Never use git stash, rebase, clean, reset --hard, or path restoration.
  3. Stage explicit assigned paths only. Never git add -A/./*, commit -a/-am.
  4. Never mutate the shared project index, sprint state, or a plan other
     than the one you are landing against. Return outcome data instead.
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
