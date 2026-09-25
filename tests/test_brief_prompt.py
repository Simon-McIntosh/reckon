"""A brief dispatch composes its task authority from the brief it was given.

A node dispatched with a brief names no committed plan section, so the composed
prompt must carry the brief text where the plan-section read instruction sits
and must not tell the worker to land its record in a plan section. Every other
part of the contract is supplied whichever carrier the node has: the four
fences, the durable-write and falsifiable-evidence contracts, the manifest
contract, the negative-control declaration and the landing contract's own
rules. A plan dispatch's prompt is byte-identical to the prompt composed before
the brief carrier existed.
"""

from __future__ import annotations

import sys

import reckon.crew.prompts as prompts_mod
from reckon.crew.node import NEEDS_HELP_MARKER, TaskNode
from reckon.crew.prompts import (
    BRIEF_LANDING_CONTRACT,
    CLOSURE_AUTHORITY_CONTRACT,
    DURABLE_WRITE_CONTRACT,
    FALSIFIABLE_EVIDENCE_CONTRACT,
    PLAN_LANDING_CONTRACT,
    compose_prompt,
)

# A distinctive multi-line brief, so a substring check cannot pass by accident
# on a fragment of the template and so a verbatim carry is visible in the
# prompt's own shape.
BRIEF_TEXT = (
    "Probe the paid lane's queue the way a coordinator must, then record the\n"
    "shape of what you found for the next reader."
)

# The plan carrier keeps a plan and a section even when a brief carrier is
# composed from the same node, so the brief assertions prove the plan pointer is
# replaced rather than merely absent from the node.
NODE = TaskNode(
    id="carrier-node",
    goal="compose the prompt for whichever authority carries this node",
    plan="plan-a",
    section="s3",
    role="implement",
    done_when="the composed prompt matches the authority given to it",
    write_paths=["reckon/crew/prompts.py"],
    time_budget="20m",
)

FOUR_FENCES = ("SCOPE", "TIME", "EVIDENCE", "DELIVERY")
PLAN_READ_INSTRUCTION = "PLAN     proj:plan-a s3"
PLAN_LANDING_INSTRUCTION = "Append your landing record to your own section of the plan"
LANDING_READER_SENTENCE = "Write your landing record and your evidence anchor"
MANIFEST_CHECK_HEADER = "CONTRACT — CHECK YOUR OWN RECORD BEFORE YOU CLOSE IT"

# The composed prompt carries the composing interpreter's own path, so the
# byte-identity check pins one interpreter for both the snapshot and the live
# composition.
SNAPSHOT_INTERPRETER = "/usr/bin/python3"


def _compose(*, brief: str = "") -> str:
    return compose_prompt(
        node=NODE,
        project="proj",
        worktree="/repo/worktree",
        working_directory="/repo/worktree",
        manifest_path="/state/runs/carrier-node/manifest.md",
        time_budget="20m",
        needs_help_after_failures=2,
        can_write_worktree=True,
        brief=brief,
    )


def _flat(text: str) -> str:
    """Collapse the prompt's manual line wrapping so a phrase can be found
    regardless of where the composer happened to break the line."""
    return " ".join(text.split())


# ── The brief is carried verbatim, in place of the plan pointer ─────────────


def test_a_brief_is_carried_verbatim() -> None:
    prompt = _compose(brief=BRIEF_TEXT)

    assert BRIEF_TEXT in prompt


def test_a_brief_prompt_carries_no_plan_section_read_instruction() -> None:
    prompt = _compose(brief=BRIEF_TEXT)

    assert PLAN_READ_INSTRUCTION not in prompt
    assert "PLAN     " not in prompt
    assert "plan-a" not in prompt


# ── The plan-landing instruction is replaced, not carried over ──────────────
#
# A brief run's record belongs in the run's own directory, so the sentence that
# sends it to a plan section must be absent while the landing contract itself is
# still declared.
#


def test_a_brief_prompt_carries_no_plan_landing_instruction() -> None:
    prompt = _compose(brief=BRIEF_TEXT)

    assert PLAN_LANDING_CONTRACT not in prompt
    assert PLAN_LANDING_INSTRUCTION not in prompt
    assert "your evidence anchor to the cumulative evidence record" not in prompt


def test_a_brief_prompt_keeps_a_landing_contract_naming_the_run_directory() -> None:
    prompt = _compose(brief=BRIEF_TEXT)
    flat = _flat(prompt)

    assert "CONTRACT — LANDING YOUR RECORD" in prompt
    assert BRIEF_LANDING_CONTRACT in prompt
    assert _flat(LANDING_READER_SENTENCE) in flat
    assert "into this run's own directory" in flat
    assert "rather than into a plan section" in flat


def test_the_brief_landing_contract_keeps_the_figure_and_meta_rules() -> None:
    flat = _flat(_compose(brief=BRIEF_TEXT))

    # The two landing contracts differ only in where the record goes.
    assert "docs/figures/<topic>/" in flat
    assert "src /<project>/figures/" in flat
    assert "never an image of what is naturally a table" in flat
    assert "Do not edit the plan-version or plan-modified meta lines" in flat


def test_the_brief_landing_contract_is_a_pure_removable_substitution(
    monkeypatch,
) -> None:
    """Mask the brief contract and recompose: the brief prompt must differ from
    the live one by exactly that block, and the plan prompt must never read the
    brief contract at all."""
    after = _compose(brief=BRIEF_TEXT)
    assert after.count(BRIEF_LANDING_CONTRACT) == 1

    monkeypatch.setattr(prompts_mod, "BRIEF_LANDING_CONTRACT", "")
    before = _compose(brief=BRIEF_TEXT)

    assert after.replace(BRIEF_LANDING_CONTRACT, "", 1) == before
    assert PLAN_LANDING_CONTRACT in _compose()
    assert BRIEF_LANDING_CONTRACT not in _compose()


# ── Everything else in the contract reaches a brief node too ────────────────


def test_a_brief_prompt_keeps_the_fences_and_the_shared_contracts() -> None:
    prompt = _compose(brief=BRIEF_TEXT)

    for fence in FOUR_FENCES:
        assert f"FENCE — {fence}" in prompt
    assert DURABLE_WRITE_CONTRACT in prompt
    assert FALSIFIABLE_EVIDENCE_CONTRACT in prompt
    assert "MANIFEST (write exactly these keys" in prompt
    assert MANIFEST_CHECK_HEADER in prompt
    assert CLOSURE_AUTHORITY_CONTRACT in prompt


def test_a_brief_prompt_keeps_the_manifest_delivery_and_the_escape_hatch() -> None:
    prompt = _compose(brief=BRIEF_TEXT)

    assert "/state/runs/carrier-node/manifest.md" in prompt
    assert "BEFORE finishing" in prompt
    assert f"`{NEEDS_HELP_MARKER} <one line>`" in prompt


def test_a_plan_prompt_carries_no_brief_carryover_marker() -> None:
    prompt = _compose()

    assert "\nBRIEF\n" not in prompt
    assert BRIEF_TEXT not in prompt


def test_the_brief_carrier_is_selected_by_a_non_empty_brief_only() -> None:
    plan_prompt = _compose()

    assert _compose(brief="") == plan_prompt
    assert BRIEF_LANDING_CONTRACT not in plan_prompt
    assert PLAN_LANDING_CONTRACT in plan_prompt
    assert _compose(brief=BRIEF_TEXT) != plan_prompt


# ── The plan prompt is byte-identical to its pre-brief form ─────────────────


def test_a_plan_prompt_is_byte_identical_to_that_snapshot(monkeypatch) -> None:
    monkeypatch.setattr(sys, "executable", SNAPSHOT_INTERPRETER)

    assert _compose() == PLAN_PROMPT_SNAPSHOT


# The composed plan-carrier prompt as produced before the brief carrier existed,
# taken under SNAPSHOT_INTERPRETER with the node, project, worktree, manifest
# path and budget this module composes with. A change to the plan path that the
# assertions above do not name fails here.
PLAN_PROMPT_SNAPSHOT = """You are a worker on one node. Read the live plan first; it is the
semantic authority for context, decisions, evidence inputs and constraints.

CONTRACT — DURABLE WRITES
  Commit each deliverable as it completes rather than once at the end, and
  write your manifest carrying whatever you already hold before beginning
  any long output. This is recovery, not death prevention: a durable write
  preceding a long generation survives that generation failing.

CONTRACT — EVIDENCE THAT COULD HAVE FAILED
  Confirm a previously recorded defect still reproduces before repairing it and
  quote the reproduction; where the behaviour is already correct, verify it and
  add the missing test rather than reimplementing a working guard.
  A passing suite never shows that a guard fires, so make the guarded thing
  happen and show the refusal.
  Treat an implausible measurement as a claim about the instrument first: a
  zero, an empty result or a uniform column needs the check shown to see
  something known present before an absence is reported.
  Read the receipt rather than the absence of an error, and verify a change
  landed by a marker it introduced rather than one it removed.
  Record the commits and paths you changed; a coordinator cannot see your
  tree, so an unfilled field reads as no work.

NODE     carrier-node
GOAL     compose the prompt for whichever authority carries this node
PLAN     proj:plan-a s3
ROLE     implement


CONTRACT — LANDING YOUR RECORD
  Append your landing record to your own section of the plan and your evidence
  anchor to the cumulative evidence record; both live in this worktree and both
  go into your final commit.
  Use a figure wherever a spatial, plotted or sequential relationship is clearer
  shown than described, under docs/figures/<topic>/ with the project-absolute
  src /<project>/figures/...; never an image of what is naturally a table.
  Do not edit the plan-version or plan-modified meta lines: every worker
  touching them makes every merge conflict there.

CONTRACT — WHO CLOSES THE NODE
  A worker does not close its own node: it must not resolve its own
  driving followup and must not set a terminal status, because only the
  coordinator observes the other nodes — a worker knows its node landed,
  not whether the section closed.

FENCE — SCOPE (exclusive write paths; nothing outside them)
  reckon/crew/prompts.py

CONCURRENT NODES (never touch their paths; request a scope change instead)
  none

PEER CHANNEL — knowledge only; write scopes never transfer. Run ; endpoint /state/runs/carrier-node/peer-channel.
  Adjacent peers: none yet; later adjacent dispatches appear in peers.json
  Client prefix: /usr/bin/python3 -c 'from reckon.crew.dispatch import _peer_command; raise SystemExit(_peer_command())'
  List/ask operands: peer-list --run  OR peer-ask --run  --peer <run-or-node> --question "<question>"
  Read/reply operands: peer-read --run  --question-id <id> --wait <duration> OR peer-reply --run  --question-id <id> --answer "<answer>"
  Reads block on filesystem events; expiry writes NEEDS-HELP to the manifest.

FENCE — TIME
  20m. Exceeding it means stop and report, never push on. Your process ends when this turn ends: never wait across a backgrounded command. Write your manifest with what you know now before starting one, and update it afterward if a later turn arrives — that is keyed to starting the wait, not to finishing the work. A run that ends mid-wait is resumable, so if you run out of turns, leave a record naming exactly what you were waiting for — set status: waiting and fill the wait_condition, wait_probe, wait_terminal and resume_brief keys in the manifest block so a resume sweep can wake you. Declaration is for a wait you hold: the wait block states something you are actually waiting on, never a note about where you are. A resumed worker whose wait is met has nothing left to wait on: record where work stands under the checkpoint key and continue, rather than writing the wait block again. So does any worker recording progress at any point, not only a resumed one.

FENCE — EVIDENCE (this measure is the done-when; state it quantitatively)
  the composed prompt matches the authority given to it This gate measures only this node's own change; verifying the merged result belongs to a separately dispatched test node, and a failure outside this node's declared scope is reported under follow_ons rather than triaged or fixed.

FENCE — DELIVERY
  Write your manifest to /state/runs/carrier-node/manifest.md BEFORE finishing, then reply with that path and a summary.
  If that exact path is not writable, STOP and report a blocker; a manifest written anywhere else means delivery cannot be found. Long output and logs go on disk.

CONTRACT — CHECK YOUR OWN RECORD BEFORE YOU CLOSE IT
  Before you set status to a terminal value, run this and repair what it
  reports — the write-time audit of your manifest against your own node
  and worktree:
  `reckon crew check-manifest --run <this run's id>`
  The same refusal at promotion reaches a coordinator hours later, after
  you have ended, and a record repaired then is indistinguishable from a
  fabricated one.
  Two fields have refused promotions on shape alone while the work behind
  them was sound. `status` must be one of the recognised values: a value
  outside that vocabulary leaves the whole record unreadable and every
  other field unread with it. `negative_control_log` must be the bare path
  and nothing else on that line: an empty field, or one carrying a
  description or a quoted first line, names nothing a reader can open.

CONTRACT — THE NEGATIVE CONTROL THIS NODE DECLARES
  None was declared on this node, so this dispatch states no string for a red
  log's first line to repeat. If you apply a mutation of your own to falsify a
  check you add, record it in the manifest anyway.

MANIFEST (write exactly these keys; after reading the plan, observe path and revision in the assigned tree and make these first three lines your first write; those three lines are the orientation write — record them under status: in-progress with a checkpoint line, and leave the wait fields empty until you are actually waiting on an external condition)
  orientation_worktree: <output of pwd>
  orientation_base_sha: <output of git rev-parse HEAD>
  orientation_write_paths: ["reckon/crew/prompts.py"]
  node: carrier-node
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
  failure_attribution: <armed-only, test role JSON {failure_id: candidate_commit} for each newly added failure>
  artifacts: <paths plus headline metrics>
  evidence_inputs: <facts the orchestrator needs for writeback>
  follow_ons: <work you found but were fenced out of, or none>
  blockers: <none, or the exact unmet condition>
WORKTREE AND PARALLEL-SAFETY RULES (binding)
  1. Work only in /repo/worktree. Do not create, checkout or switch branches.
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
`NEEDS-HELP: <one line>` followed by all four of:
  tried:         what you attempted and the observable result
  options:       two or three concrete paths you can see
  leaning:       which one, and why
  cost-if-wrong: what must be redone if the wrong path is taken
Stop on any of: the same command failed 2 times with
different fixes attempted; a decision the plan does not settle is required;
the necessary change exceeds your write scope; the evidence cannot be produced
with the tools or data available; the time budget is spent with the measure
still unmet. Asking costs one turn; thrashing costs the node.
"""