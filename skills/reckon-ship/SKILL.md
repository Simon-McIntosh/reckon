---
name: reckon-ship
description: >-
  Execute a complete Reckon plan, or coordinate an entire sprint, without doing
  implementation inline. Resolves a plan slug with an optional section,
  `/reckon-ship S1`, and a project-qualified sprint id. Both targets are strictly
  coordinator-only, a one-node plan included: build the execution DAG, delegate
  every implementation, investigation, test, pipeline, and repair node through
  isolated worktrees by default, audit and integrate worker commits, record
  outcomes continuously, and clean up worktrees. Trigger verbs: "implement /
  execute / ship / land / deliver the sprint / run the sprint / /reckon-ship".
  For editing plan text use reckon-edit; for defining or rebalancing sprint
  state use reckon-sprint.
allowed-tools: Read Write Edit Bash(*) Grep Agent mcp__reckon___read_plan mcp__reckon___edit_plan mcp__reckon___roadmap mcp__reckon___audit
---

# reckon-ship — execute a complete plan or sprint and record outcomes

## Critical behaviour: resolve the target, then finish its executable scope

There are two execution targets:

- **Single-plan target:** `/reckon-ship <slug>` delivers the entire plan;
  `/reckon-ship <slug> §N` delivers only the named section.
- **Sprint target:** `/reckon-ship S1` executes the current project's sprint;
  `/reckon-ship <project>:S1` selects a project explicitly. It reads every
  sprint plan, transitive dependencies, linked research, and prior evidence,
  then coordinates a rolling queue of ready nodes. Use `plan:<slug>` or `sprint:<id>`
  only to disambiguate unusual identifiers.

On a single-plan target without a section, you MUST:
1. Read the complete plan HTML and classify every section
2. Identify ALL implementable sections (not deferred/blocked/done)
3. Delegate them all — sequentially for dependent sections, in a parallel fleet for independent ones
4. Promote each landed node and write its plan state in the same beat, then continue
5. Stop only when all implementable sections are done OR you hit a hard prerequisite blocker

The sprint invocation authorises the listed plans and their actionable
same-project prerequisites; it does not broaden authority to unrelated projects,
external systems, destructive actions, or new outward-facing effects. Reckon
composes the dispatch contract; consult `references/sprint-orchestration.md`
only when hand-composing a delegation Reckon did not prepare.

Use `roadmap(project, sprint=<id>)` as the canonical plan-level graph. Do not
rebuild plan dependency order from repeated discovery calls. Add section-level
and file-conflict edges only after the tool returns no error-level wiring
findings.

### Both targets are coordinator-only

**One contract, both targets, a one-node plan included.** Whichever target resolved,
preserve coordinator context for orchestration. The coordinator may resolve/read
state, checkpoint the DAG/scopes, create worktrees, dispatch or message workers,
audit evidence, integrate/push, write state, clean, and report.

The coordinator MUST NOT implement, investigate implementation details beyond
scoping/review, edit product/source/test files, execute tests or operational
pipelines, or repair worker code. Every implementation, investigation, test
execution, operational pipeline run, and corrective repair is a worker node,
even when only one item is ready and a worker slot is available. On failure,
redispatch a corrective worker. The detailed contract, manifest, and checkpoint
discipline live in `references/sprint-orchestration.md`.

**A small plan is the case this rule exists for, not an exception to it.** One
node is where inline is cheapest to rationalise and costs the most: the work
lands in coordinator context that then cannot review it, no worktree bounds the
blast radius, no manifest records what happened, and the run never reaches the
ledger — so the node is invisible to calibration and to the next session. A
single well-formed node dispatches in one call. Node count changes the active
fleet, never whether a node is delegated.

Inline fallback is exceptional and is a reported event, not a judgement call: use
it only when no capable worker or slot exists, say so and context-budget it
before implementing, and prefer pausing the node. "The plan is small", "this is
one file", "dispatch overhead exceeds the work" and "I already have the context"
are not the exception — they are the rationalisation the exception is worded
against.

### Continuity — who receives the next piece of work

A node's worker holds context no fresh worker can rebuild cheaply. Route by what
the next piece of work *is*, not by whichever worker is convenient:

| Next work | Goes to | How |
|---|---|---|
| A `NEEDS-HELP:` brief from a CLI-launched live run | that same run's session | `reckon crew resume --run <id> --advice "…"` |
| A `NEEDS-HELP:` brief from an in-harness run | that attached harness task/session | answer it through the host harness; CLI resume cannot launch it |
| A followup on work that just landed — review comment, gate evidence, a fix within the node's own scope | the **same worker**, via its roster member's long-lived session | `reckon crew dispatch … --member <id>` |
| New scope, a different file set, or significant rework | a **fresh dispatch**, its own worktree and node | `reckon crew dispatch …` with a new node id |

**This works only if the original dispatch named `--member`.** `reckon crew
complete` deletes the live pointer, taking the run's `session_id` with it, so
after promotion the session survives in exactly one place: the roster entry in
committed `crew.json`, where `capture_session` recorded it on the member's first
run (first id wins). **So dispatch every node as a roster member by default** —
`reckon crew member list --project <project>` shows the team, `reckon crew member
add` registers a new one, and a member registered with no session captures one
from its first run.

The boundary between the middle row and the last is scope, not size. Work that
stays inside the landed node's declared write paths and its gate goes back to the
same member. Work that widens the scope is a new node — dispatching it into an
old session hides a scope change inside a session that was fenced for something
else, and a scope change is exactly what `--scope-changed` exists to record.

**A member is a serial worker, so size the active fleet in members, not in nodes.**
Before creating a worktree, dispatch refuses a member that already owns a
non-terminal live pointer. The typed refusal names both the member and the
in-flight run. Observe or recover that run, finish or intentionally stop it, and
promote its result before reusing the member. Use a distinct roster member for
independent concurrent work; do not hide a continuation inside an unmembered
fresh session merely to bypass the guard.

**Do NOT stop at routine checkpoints.** Keep going and update state as work
lands. Valid early stops are:
- A prerequisite plan is unshipped (hard stop — see §Prerequisite blocking)
- A NEW decision surfaced that is not already locked in the plan, is material to the work, and cannot be deduced from the plan/code/sensible defaults (an already-locked decision is NOT a reason to stop — honour it and proceed)
- The next section's scope would require writing files outside your allocated write scope
- Applicable safety policy or user authority requires confirmation
- A worker commit cannot be integrated safely without overwriting unrelated work

### These are not valid reasons to stop

Continue through ordinary complexity, validation, and recoverable integration:

| Rationalization | Reality |
|---|---|
| "This change is high-blast-radius / touches core code" | Allocate it to an appropriately capable worker, test it, and validate integration. |
| "Better to confirm the approach before executing" | The plan IS the approved approach. Locked decisions ARE the confirmation. Asking again is re-litigating settled decisions. |
| "This is a lot of work / the session is long / I've done enough" | Length and effort are not blockers. Continue until every implementable item is done or you hit a valid stop. |
| "It needs full-suite validation first" | Then dispatch a test worker — validation is part of the work, not a reason to hand back. |
| "I'll present options A/B and let the user choose" | If the plan already determines the path, there is no choice to present. Pick the plan's path and execute. Offering A/B on already-decided work is a checkpoint in disguise. |

Plans do not override global safety or expand user authority. A locked decision
settles implementation choices only inside the already-authorised scope.

## Fast path

```text
resolve target
├─ plan → roadmap + full plan → classify sections → delegate in dependency order
└─ sprint → roadmap + all plans/research/evidence → enrich DAG → run ready queue
     ↓
read task requirements + apply explicit runtime routing + applicable skill
→ audit plan currency against the code + dispatch the prior-art scout (reuse map)
→ check every node against the seven-property contract (§3b)
→ reckon crew preflight — a spent backend holds its nodes, the rest still run
→ reckon crew dispatch each ready node — branch only on the returned launch kind
→ emit the dispatch summary, naming the gate that closes the wave
→ audit worker manifests/commits/tests → orchestrator merges
→ reckon crew complete each run — the record becomes committed evidence
→ immediately write that node's commit, gate measure, artifacts, and impl to the plan
→ emit the completion summary, WHY carrying the gate evidence
→ re-triage open followups + manifest follow_ons; fold them into the DAG until dry
→ record plan/evidence/sprint outcomes + continuation at all three altitudes
→ prove commits reachable → remove worktrees → close sprint when complete
```

Full detail below.

## When to invoke

- "implement / execute / ship X" / "land items from X" / "do the work in X plan"
- `/reckon-ship <slug>` — implements the WHOLE PLAN
- `/reckon-ship <slug> [§N]` — implements only the named section
- `/reckon-ship S1` — executes sprint `S1` in the current project
- `/reckon-ship <project>:S1` — executes a sprint in an explicit project
- Reading a §05 followup whose `recommends_skill` is `/reckon-ship`

**Dual-role:** invoked by human or orchestrator AND records one-line §05 session handoffs.

If the user wants to *write* the plan → `reckon-edit`. Plan doesn't exist → `reckon-create` first.

## The model — the plan HTML is the document AND the store

**The plan HTML is the source of truth.** Read it first — ALL of it. Both targets
delegate what it describes and coordinate the outcomes; the modes differ in the
scope they resolve, never in who does the work. The HTML documents the work;
the `data-reckon` sections carry
structured state (decisions, followups). Do not implement items marked
"deferred", "post-v1", or behind an unmet trigger.

**Write path:** use `edit_plan` to record outcomes atomically:
1. `read_plan(resource={project,type:"plan",id:slug}, view="raw")` → get `version`.
2. `edit_plan(…, ops=[set status/impl + resolve driving followup + append next followup], expected_version=…)`.
3. On 412 conflict: re-read + retry.

## Hard rules

1. **Read the FULL selected scope before ANY dispatch.** On a single-plan target, read the complete plan. On a sprint target, read the sprint index, every member plan, transitive dependency, linked research document, and prior evidence record before dispatch.
2. **Full plan by default.** `/reckon-ship <slug>` without a section flag means ALL implementable sections. Never implement one section and stop unless there is a hard blocker.
3. **Whole sprint by default.** `/reckon-ship S1` means every executable item in the sprint plus actionable same-project prerequisites.
4. **Coordinators delegate every executable node, on both targets.** This includes a plan holding exactly one node, investigation, test execution, operational pipelines, and corrective repair. Inline is the reported exception of §Both targets are coordinator-only, never the default for small work.
5. **Verify every worker.** Retrieve its compact manifest, audit `git show --stat <sha>` against declared scope, and ensure relevant tests ran before integration. Test execution is itself a worker node.
6. **Scope allocation precedes dispatch.** Use isolated worktrees by default; list each worker's exclusive write paths before sending a prompt. No two workers share a file.
7. **The portable dispatch contract is mandatory.** `reckon crew dispatch`
   composes it. Read and embed the reference contract only when hand-composing a
   delegation Reckon did not prepare.
8. **Update the plan at every node landing.** Immediately after EACH
   `reckon crew complete`, the orchestrator updates the cumulative evidence and
   calls `edit_plan` once with the node's commit, gate measure, artifacts, and
   advanced `impl`. Nothing else may be promoted or merged before this write.
   Dispatch of an unrelated ready node may continue; it does not wait on this
   plan-write beat. Collapse the evergreen section only when its final node lands;
   never wait for section closure to record earlier nodes.
9. **One cumulative evidence record and a followup are required.** Default to
   `docs/evidence/archive/<slug>-landed.html`, carrying
   `reckon-type=evidence` and `plan-evidence-for=<slug>`, with one stable anchor
   per material result. Update that record after each landing. Do not create a
   file per section, commit, test wave, or one-line outcome. Create another
   evidence resource only when it is a materially independent artifact useful
   on its own.
10. **Collapse the evergreen when a section ships.** Replace the section body
    with a 2-4 line landed-summary + link to the matching anchor in the
    cumulative evidence HTML.
11. **No plan-state drift.** Plan and sprint state must reflect reality at the end of every turn.
12. **The sprint coordinator owns only coordination, integration, and shared state.** Workers commit implementation and verification work in detached worktrees; they do not merge, push the primary branch, or mutate the shared index/plan state.
13. **Cleanup is mandatory and conservative.** Remove a worktree only after it is clean and its commit is reachable from the integrated primary branch. Never force-remove unmerged or dirty worktrees.
14. **Do not execute a malformed graph.** Invalid, dangling, non-executable,
    inactive, contradictory, cyclic, sprint-order, or membership findings are
    wiring repairs, not scientific blockers and not override prompts.
15. **Drain foldable followups before closure.** Re-triage open followups after
    every landing beat, route that node's manifest `follow_ons` through the same loop,
    and repeat until a complete pass finds nothing foldable. Never set a plan
    to `shipped` or `done` while a foldable followup is open.
16. **No implementation node before the reuse map exists.** Every substantive
    implementation wave is preceded by a prior-art scout (§1b) whose reuse map
    the nodes cite; a node that authors new machinery states why each named
    reuse candidate fails. Trivial mechanical edits are exempt; "the scout
    would slow us down" is not.

## §Prerequisite blocking — STOP and ask for authorization

On a single-plan target, an unshipped prerequisite remains a hard stop unless the user
authorises implementing or overriding it. On a sprint target, actionable
same-project prerequisites become nodes in the execution DAG automatically.
Stop for cross-project, unavailable, abandoned, or authority-expanding
prerequisites.

`depends_on` entries may be EXTERNAL — `project:slug[#stage]` refs into
another mounted project (bare slugs stay local). `read_plan(project, slug)`
returns a computed `deps` list resolving every ref (`scope`, `found`,
`status`, `impl`); gate on that instead of assuming a bare slug is local. An
unshipped external prerequisite is a hard stop like any other, but its work
belongs to the OTHER project's checkout — never implement it in this one;
surface it as `/reckon-ship <project>:<slug>`.

Before applying this stop, inspect `roadmap.wiring_findings`. A research or
evidence artifact in `depends_on` is not an unmet executable prerequisite; move
that edge to `informs`. A superseded umbrella, dangling slug, cycle, or
sprint-order inversion is also a plan-state defect. Repair it through
`reckon-edit` when authorized, re-run `roadmap`, and only then classify the
remaining unresolved rows as true prerequisites. Never ask the user to
override a relationship that Reckon identifies as malformed.

For a plan-mode stop, ask for explicit user authorization:

```
⛔ BLOCKED: cannot implement <slug> — prerequisite unmet

The plan '<slug>' depends on '<prereq-slug>' which is currently status='<status>'.

To proceed, one of the following is needed:
  A) Implement '<prereq-slug>' first: run /reckon-ship <prereq-slug>
  B) Manually mark '<prereq-slug>' as done if it is already complete
  C) Override the dependency (confirm you want to proceed without it)

Please authorize one of the above before I continue.
```

Wait for the user's response before doing anything else. If the user authorizes option A, switch to implementing the prerequisite first, then return to the blocked plan.

## Workflow

### 0. Resolve plan vs sprint

1. Derive the current project from the repository/mount context.
2. Call `roadmap(project)` and match the argument against exact sprint ids.
3. Treat an exact sprint match, `sprint:<id>`, or `<project>:<id>` as sprint
   a sprint target. Treat `plan:<slug>` or every other slug as a single-plan target.
4. If a sprint target, resolve the sprint and use the same coordinator workflow
   over its complete graph. Do not continue with the plan-only preflight below.

### 1. Plan pre-flight — read the FULL plan

**This step is NON-NEGOTIABLE. Do not skip it. Do not begin implementation until it is complete.**

Call `roadmap(project)` first. Require the target to be in `ready_now`, or
follow its earliest local prerequisite from the returned critical/open path.
Resolve every error-level wiring finding before implementation.

```python
# Read ALL current plan state, then read the response/storage contract
state = read_plan(
    resource={"project": "<project>", "type": "plan", "id": "<slug>"},
    view="raw",
)
contract = read_plan(
    resource={"project": "<project>", "type": "plan", "id": "<slug>"},
    view="schema",
)

# Also fetch the raw HTML to read section prose
# (the MCP payload has parsed state; you also need the full prose sections)
# Use: curl http://127.0.0.1:8765/<project>/plans/<slug> OR read the discovery row's href
```

Then read the COMPLETE HTML file from disk:
```bash
# Read the full plan HTML — every section, every paragraph
cat docs/plans/<slug>.html
```

Do not proceed until you have read and understood:
- Every `<h2>` section and its prose
- All `<section data-reckon="decisions">` items (locked and open)
- All `<section data-reckon="followups">` items (resolved and open)
- The `plan-depends-on` meta tag
- Any `Trigger:` subsections or deferral markers

### 1b. Currency audit and prior-art reconnaissance — MANDATORY

Plans go stale, and agents are quick to rebuild what already exists. Between
reading the plan and authoring any implementation node:

**Audit plan currency against the code.** Every mechanism the plan asserts —
an API, a kernel, a file, a claimed limitation, a mathematical property — is
checked against the tree it names before nodes are cut from it. A node
authored from stale text inherits its defects and executes them faithfully.
(Incident 2026-08-21, nova: a section specified clipping to an "arc turning
point" of a bilinear level set; the derivative's numerator is a constant, so
no interior turning point exists. The worker built the correction exactly,
validated it to 1.6e-5, and measured the result worse on every decisive
field. The plan text, not the worker, was the defect.)

**Dispatch a prior-art scout in the background.** One read-only
investigate-role node (or an Explore agent when worker slots are scarce),
launched at pre-flight so it runs while the coordinator finishes reading
state. Its single deliverable is a REUSE MAP: the modules, symbols, tests and
data already in reach that solve the problem in whole or in part, each with a
one-line fitness verdict. The scout searches:

- this repository, capability-shaped rather than filename-shaped ("2-D
  interpolation", "polygon clipping", "contour tracing" — not "does
  fsa_kernel.py exist");
- every repository named by the plan's external `depends_on` / `blocks`
  refs; and
- every repository this repo's AGENTS.md declares as coupled (e.g.
  nova ⇄ imas-ambix) — coupling runs both ways, so an ambix plan searches
  nova and vice versa.

**Nodes cite the reuse map.** Each implementation node's goal/prompt names
the existing machinery it extends or consumes. A node that authors new
machinery states why each named reuse candidate fails — "did not look" is the
failure mode this step exists to remove. (Incidents 2026-08-20, nova: a node
was dispatched to invent a sub-cell quadrature stencil while
`separatrix_clip.TracedClippedSupports` already carried exact clipped-cell
moments two packages away; a smeared surface-averaging kernel had earlier
been authored while a contour-traced extraction and a contourpy wrapper both
already existed in the same repo. Each duplicate was caught by the user, not
by process.)

The scout costs minutes of read-only time. The failure it prevents is not
just wasted effort: a duplicate implementation becomes a second authority
that later disagrees with the first, and reconciling two authorities costs
more than either did.

### 2. Classify ALL items

Build a complete audit before implementing anything:

| Signal | Action |
|---|---|
| Past-tense prose / commit SHAs present | Skip — already done |
| Marked "deferred", "v1", "post-smoke" | Skip — note it |
| `Trigger:` subsection with unmet condition | Skip — surface to user |
| `depends_on` prerequisite not shipped | **STOP — ask user for authorization** (see §Prerequisite blocking) |
| Concrete deliverable, no deferral signal | Implement |

**Report a complete audit before dispatching a single worker:**
```
Audit for <slug>:
  Implementable: §2 (3 items), §3 (2 items), §4 (1 item)
  Deferred:      §5 — marked post-v1
  Already done:  §1 — commit abc1234 present in prose
  Prerequisites: CLEAR (no depends_on / all satisfied)

Dispatch plan:
  §2: fleet of 3 (parallel) — workers A/B/C
  §3: fleet of 2 (parallel) — workers D/E
  §4: one worker — 1 item
  Sequential order: §2 → §3 → §4 (§3 depends on §2 output)
```

### 3. Scope allocation

List **exclusive write paths** per item. If two items share a file, serialise them or split it (`test_a.py` / `test_b.py`).

**Never dispatch two workers that write the same file.**

### 3b. Pre-dispatch checklist — a node is well-formed BEFORE it is sent

**A worker that thrashes is almost always executing a malformed task.** Check
well-formedness before dispatch, not after; that leaves the escape hatch handling
only the genuine residual. A node that fails is reshaped or split — never
dispatched in the hope that the worker will work it out.

All seven must hold:

- [ ] **Single goal** — one deliverable. Joined with "and" means two nodes.
- [ ] **Fully specified** — every input is in the live plan or in the fences;
      nothing requires the worker to infer intent.
- [ ] **Demonstrable** — done-when is a *measure* that emits evidence: a named
      test, a recorded command output, a number against a stated bound. A
      subjective adjective ("clean", "robust", "better") fails.
- [ ] **Closed** — no decision from outside the node is needed; a
      required-but-unlocked decision means a decision node precedes it.
- [ ] **Scoped** — exclusive write paths enumerated, none shared with a
      concurrent node.
- [ ] **Bounded** — fits the resolved time budget. If it cannot, split it; a
      budget is not a target to overrun.
- [ ] **Independently verifiable** — auditable from the manifest, `git show
      --stat` and the gate evidence, without reading the implementation.

`reckon crew dispatch` enforces the same seven and exits 2 naming every failing
property. Check every node before it enters the ready queue:

```bash
reckon crew dispatch --project P --plan L --section §N --role implement \
  --node <id> --goal "<one deliverable>" --done-when "<measure>" \
  --write-path <path> --session <session> --dry-run
```

The repository must contain
`skills/reckon-ship/scripts/worktree_fleet.py` before dispatch can create an
isolated worktree. If it is absent, run `reckon sync docs/` from the repository
root and retry; the command refuses before creating a run.

Dispatch uses these process exit codes. Treat a refusal as an instruction to
repair the request, not as a worker failure:

| Result | Exit | Remedy |
|---|---:|---|
| `success` | 0 | Continue with the returned launch contract. |
| `request-error` | 1 | Correct malformed options or configuration and retry. |
| `not-dispatchable` | 2 | Repair the node contract named in `validation`. |
| `budget-hold` | 3 | Keep the node ready and retry after the reported reset. |
| `plan-unavailable` | 4 | Commit the plan before dispatching so its named section exists identically at the base revision. |
| `competence-refusal` | 5 | Route the node to a backend meeting the reported capability requirements. |

Three node-contract refusals are easy to mistake for infrastructure failures:

- A dirty plan HTML file cannot supply base-revision authority. Commit the plan
  before dispatching; `plan-unavailable` names the missing or changed section.
- A goal containing `;` is not one deliverable. Rewrite it as one outcome; use
  the DAG for sequential work. Action-bearing `and`, `&`, or `plus` clauses are
  refused for the same reason.
- Every node needs at least one `--write-path`. For verification, cleanup, or
  other work with no tracked repository output, name the on-disk report, log, or
  artifact path that constitutes its exclusive delivery scope.

The engine supplies the full contract, manifest shape, and escape hatch. Read
`references/worker-protocol.md` only when hand-composing a delegation Reckon did
not prepare.

### 4. Dispatch workers

Pre-flight the routing surface once per session with `reckon flight --project P`:
it reports the resolved config, which layer supplied each value, and which
backends are actually available.

**One dispatch instruction covers every backend.** State what the node is;
reckon resolves how it runs. Which harness, at what model, effort and sandbox
tier, comes from flight config — so this skill names none of them, and a
per-task deviation is an override on the same call rather than a different call:

```bash
reckon crew dispatch --project P --plan L --section §N --role implement \
  --node <id> --goal "<one deliverable>" --done-when "<measure>" \
  --write-path <path> --peer <other-node>=<their-paths> \
  --time-budget 25m --session <session> [--set backends.<name>.effort=high]
```

**Branch once, on the returned `launch` kind, and never on anything else:**

| `launch` | What reckon did | What you do |
|---|---|---|
| `cli` | created the worktree, spawned the worker, wrote the run record | background the call, yield, then `reckon crew observe --run <id>` |
| `in-harness` | prepared the worktree, manifest path and fences, returned a directive | dispatch your own delegation primitive against the directive, then `reckon crew attach --run <id> --task <task-id>` |

A CLI process can be resumed or stopped with `reckon crew resume` and `reckon
crew stop`. An in-harness run has no spawned process for those commands: continue,
answer, or cancel it through the attached harness task/session. Roster continuity
for an in-harness run likewise belongs to that host session; the CLI only records
the attachment and its observations.

A fresh session that inherited runs it did not dispatch starts with `reckon crew
recover`, not with a redispatch: it classifies every live pointer as running,
completed-but-unpromoted, or abandoned, and names the next action for each.

Adding a harness never adds a third case. Do not read backend flags into a
prompt: per-backend translation is compiled code, which is what makes drift
between execution paths impossible to express.

`reckon crew observe --run <id>` folds the worker's stream, manifest presence and
process liveness back into its record — phase, captured session id, and whatever
budget signal the backend emitted, which may legitimately read `unknown`. **Never
read an absent budget signal as exhaustion.**

### Reading run state — MCP owns reads, the CLI owns actions

**To see how the fleet is doing, call the `crew` MCP tool. Do not shell out.**
The split is a locked decision, not a style preference: the CLI exists for the
things that change the world — `dispatch`, `attach`, `resume`, `stop`,
`complete`, `member add` — and every *read* belongs to the tool.

```text
crew(project, view="live")      every run in flight: node, plan, phase, process_alive,
                                manifest_present, worktree, its recover classification,
                                and the next action for each — one call, no worker touched
crew(project, view="ledger")    committed run records
crew(project, view="summary")   roster, gate outcomes, measured time against declared effort
crew(project, view="flight")    resolved routing, and which layer supplied each value
crew(project, view="records")   lossless committed run records for detailed audit
crew(project, view="budget")    backend headroom, hold state, reset time, and dispatch ceiling
```

`view="live"` answers "are my background workers alive, and where are they" for
the whole fleet at once. Reaching for `reckon crew list`, a `stat` on a stream
file, or a `ps` grep instead is a sign you skipped the tool — those give you less,
one run at a time, and a `ps` grep cannot tell your workers from a peer session's.

Two things the tool cannot do for you. **`phase` is read from the stored record,
so it lags** — a run deep into its work still reads `starting` until an `observe`
folds its stream, while `process_alive` and `log_age_seconds` in the same payload
are always fresh. Run `observe` first when the phase matters. And **`observe` is
also what captures the run's token usage**, so promoting without it records
`tokens: null` even though the stream held them all along.

### Arm a watch when you dispatch, and watch the manifest's status

**Nothing tells you a worker stopped.** Run state is pull-only: a worker that
finishes, blocks, or dies changes a file, and you find out when you next look. So
arm the watch as part of dispatching — not after, not when you remember. A node
dispatched with nothing waiting on it is a node you will discover by accident,
and the accident is usually someone asking why nothing has happened.

**Wait on the manifest's `status:` line, never on the file existing.** A blocked
manifest exists exactly like a successful one, so an existence test reports
surrender as delivery:

```bash
# wrong — fires identically for status: complete and status: blocked
until [ -f "$M/node.md" ]; do sleep 20; done

# right — distinguishes delivery from surrender, and gives up on a dead run
until grep -qE '^status: (complete|blocked|failed)' "$M/node.md" 2>/dev/null; do
  kill -0 "$PID" 2>/dev/null || { echo "process gone, no manifest"; break; }
  sleep 20
done
```

The liveness check matters as much as the grep. A watch keyed only to a file
waits forever when its run died before writing anything — which is exactly what a
session-lock collision does, and the wait is silent, so the lost node looks like a
slow one.

The live classifier reads the manifest's recorded status. `complete` becomes
`completed_unpromoted`; `blocked` and `failed` retain those classifications;
missing or unusable terminal manifests become `abandoned`. Still read the
manifest's evidence before promotion: classification distinguishes outcomes but
does not prove the gate.

### Advisory fleet-size guide

**This table is advisory.** It shapes the active fleet; it never decides whether
to delegate. That is
already settled for both targets — every ready node goes to an appropriately
capable worker, one-item and cross-cutting nodes included, rather than making the
coordinator the implementation owner. The roster of free, distinct members is
the real ceiling for session-reusing workers; available slots, dependency
independence, file scopes, gates, budgets, and runtime limits may lower it.

| Items | Strategy |
|---|---|
| 1 | One worktree worker |
| 2–8 independent | Parallel worktree fleet when workers are available |
| > 8 | Reader fan-out followed by one synthesis/integration owner |
| Cross-cutting / strategic | One highest-capability worker; do not fragment context |

Apply the model, effort, and concurrency routing stated by the current user
prompt. If it is not specified, the coordinator chooses it explicitly for each
node from the available runtime workers and records the choice in the dispatch
prompt — as a `--set` override on the dispatch call, so the choice is data rather
than prose. Reckon does not infer a relative tier from the coordinator model.
Worker prompts reference the live plan and carry only the portable runtime
safety contract; §05 followups remain one-line session invocations.

Use background mode when the runtime supports it. The current user prompt or
coordinator sets an explicit concurrency cap before dispatch from the available
slots, ready nodes, member roster, and file-scope conflicts. Fill available slots
to that cap, then refill each slot as soon as its finished node is verified and a
ready independent node exists. Do not wait for the slowest active node. The safety
rule is: **no dependent node builds on unverified work**. A dependent waits for
its predecessor's verification, integration, and landing beat; unrelated ready
work does not.

### 4b. The gate fence — work does not cross a closed gate

**Authored here and nowhere else.** Read computed gate state for the plan through
`read_plan` and `roadmap`: use the returned `blocking` and `gate_blockers` rather
than reconstructing gate verdicts from prose. Read the resolved `gates.enforce`
setting through `crew(project, view="flight")`; strict enforcement refuses a
closed gate, while advisory enforcement records a warning. Under strict
enforcement, **refuse to dispatch work behind the gate until its measure has produced
evidence.** A gate is a measure to demonstrate, not a threshold to tune around:
when it fails, downstream work stays visibly closed and the negative result stays
on the page.

- Name the gate that closes the current wave in the dispatch summary's `WHEN`
  axis, so it is stated before the work starts rather than after.
- At completion, the `WHY` axis carries that gate's evidence — quantitatively.
- A gate whose evidence cannot be produced is a `NEEDS-HELP:` report or a
  recorded negative result. It is never a wave that proceeds anyway.
- `gates.enforce` and `gates.on_fail` in flight config tune strictness; they do
  not license skipping a gate whose measure simply was not run.

### 4c. The budget fence — a ready slot does not open into a spent quota

**Authored here and nowhere else.** Before opening a wave, run the pre-flight and
**refuse to open it on a backend whose headroom is spent:**

```bash
reckon crew preflight --project P --role implement --role review
```

It exits 3 when any backend is held, naming that backend, its utilisation and
when it resets. It costs nothing to run — it reads the budget signal earlier runs
already recorded, so it spends none of the resource it is measuring.

Four properties of a hold, each the opposite of a failure mode:

- **A hold is not a failure.** No worktree is created, no node fails, nothing has
  to be unwound, and the nodes stay ready. `reckon crew dispatch` exits 3 with a
  `hold` payload for the same reason — a caller that cannot tell a hold from a
  malformed node either rewrites work that was fine or abandons work that was
  only waiting.
- **A hold is per-backend.** One spent backend must never stop ready nodes routed
  somewhere else; the pre-flight reports held and clear backends side by side, and
  the clear ones dispatch in the same wave.
- **Unknown never holds.** A backend that publishes no headroom reads `unknown`,
  and the wave opens. Absence of a signal is not evidence of exhaustion: a false
  hold is invisible and stalls everything, while the failure it would prevent is a
  rejected call that announces itself.
- **A hold is never silent.** Report it on the four axes below, with the
  occasion `hold`: what is held, why — with the figure — how it stays recoverable,
  and when it lifts. A hold nobody reported is indistinguishable from a crashed
  orchestrator.

The reserve is worth understanding rather than tuning. A fresh dispatch stops at
the ceiling *less* `budget.resume_reserve_pct`, while answering a stuck worker may
spend it — because spending the last of a quota on a new node leaves nothing to
answer a `NEEDS-HELP:` report with, which strands the wave in its worst possible
state: work in flight and no way to unblock it.

**Resuming a held wave without a human is a host capability, not a process rule.**
Whether this orchestrator can schedule its own resumption at the reported
`resume_at` depends entirely on the harness it is running inside, so it is
documented per host in `references/orchestrator-harness/<harness>.md` and nowhere
else. Read the file for the host you are on. Where no such capability exists,
report the reset time and stop — degraded, not broken, and never a reason to
dispatch into the quota anyway.

### 4d. The summary reflex — what, why, how, when

Dispatches are silent by design and workers report in their own idiom, so the
orchestrator owes the lead a readable account at four occasions: **at dispatch,
at completion, when a wave is held, and when micro-planning the next step.** One
habit, not four formats. Four lines, one per axis, at most two lines each,
restating nothing the plan already says. Here a wave is a reporting snapshot of
the active fleet, never a wait-for-everyone barrier.

```text
Dispatching wave 2 — 3 workers
WHAT   §3 dispatch primitive (impl-a) · §4 observation (impl-b) · §5 docs (impl-c)
WHY    §3 unblocks §4 and §5; all three read the §2 contract; no shared files
HOW    detached worktrees, scopes below, manifests on disk
WHEN   ~20 min each; gate g-end-to-end closes the wave — §6 stays shut until it passes

Wave 2 complete — 3/3 landed, gate g-end-to-end PASSED
WHAT   dispatch primitive + observation + docs (1a2b3c4, 5d6e7f8, 9a0b1c2)
WHY    gate evidence: node landed in its worktree, manifest on disk, 28 tests green
HOW    all scoped clean on git show --stat; no out-of-scope paths
WHEN   next §6 — ready, nothing blocks it

Wave 3 held — 1 backend held, 1 clear
WHAT   §6 integration (impl-d) held; §7 docs (impl-e) dispatching on the clear backend
WHY    held backend at 97.2% utilisation against a 95% effective ceiling
HOW    no worktree created and no node failed; both nodes stay ready
WHEN   resets 2026-08-12T18:04:00Z, in 2280s — the wave reopens then
```

`WHAT` names nodes and artifacts. `WHY` gives the causal reason this wave runs
now — **and at completion or a hold it carries the figure.** `HOW` carries runtime
and isolation facts only. `WHEN` gives a duration estimate and names the gate that
closes the wave, or the reset that lifts the hold.

That one discipline is why the format earns its place: it forces every wave
report to be quantitative, and makes a wave that cannot state its gate evidence
visibly incomplete rather than plausibly done.

### 5. Verify every worker — MANDATORY

Verify each finished worker before integrating its result or releasing dependent
work. Independent active workers do not form a barrier: no dependent node builds
on unverified work, while any free slot may refill from the ready queue.

**Read the manifest path returned by dispatch, not just the message.** Dispatch
defaults it to the durable run directory under the Reckon config home; omit
`--manifest` unless an absolute durable override is required. A background worker
can finish its work and still end its turn
without delivering a report — the runtime signals it idle and the node looks
failed when it is not. Requiring the manifest on disk removes the failure mode;
see "Durable delivery" in `references/sprint-orchestration.md`.

For each completed agent:
1. Read the worker's manifest file. Use the runtime's result/wait tool as a
   convenience — never wait on it as the sole channel
2. Check the manifest for success/failure
3. Run `git show --stat <sha>` — confirm ONLY assigned paths appear
4. Dispatch a test worker and audit its compact result manifest
5. Confirm the worker returned commit, test, artifact, and evidence inputs.
   The orchestrator writes plan/index state after integration.
6. Promote the run: `reckon crew complete --run <id> --gate <verdict> --commit
   <sha> [--tests-added N] [--scope-changed]`. This is the moment the transient
   pointer becomes committed evidence in the repository's ledger, and the last
   moment `--tests-added` and `--scope-changed` can still be stated — a
   scope-changed node measures neither the estimate nor the worker, so saying so
   keeps it out of calibration instead of averaging it in.
7. **In the same landing beat, perform the plan write in §7.** Immediately after
   `reckon crew complete`, and before another promotion or merge, the orchestrator
   writes this node's commit, gate verdict and
   quantitative measure, artifacts, and new `impl` together. Workers still only
   return outcome data; they never write shared plan state. Dispatching an
   unrelated ready node is outside this freeze and may refill a free slot.

An agent that signals idle WITHOUT a report has probably not failed. Before
redispatching: check the manifest path, then any test logs or artifacts its
prompt required, then ask it to write the deliverable to the named path and
reply with the path only. Redispatch is the last step, not the first — a
duplicate run of a node that already succeeded burns a worker slot and, with
write scope, risks a conflicting second commit.

**A report opening `NEEDS-HELP:` is not a failure — it is a decision brief, and
answering it is cheaper than any alternative.** It carries four fields: `tried:`,
`options:`, `leaning:`, `cost-if-wrong:`. Answer it yourself by default; escalate
only genuinely user-owned decisions such as scope trade-offs and irreversible
choices. Then resume the **same** session, because the advice only makes sense to
a worker that still remembers what it tried:

```bash
reckon crew resume --run <run-id> --advice "<the answer>"
```

If an agent genuinely FAILS or produces incomplete work:
- Dispatch a corrective worker; pause the node if no capable worker or slot
  exists. A repair inside the failed node's own scope goes back to its roster
  member, so the fix reaches a worker that remembers the attempt
- Do NOT proceed to the next section while a failed section's work is outstanding

Inspect only the summary, scoped diff, and evidence needed to diagnose the
failure. Sprint coordinators do not repair worker code themselves. Do not
advance the dependency wave with incomplete work.

### 6. Record outcomes — after EACH node

**Do not wait until a section is done.** Update its cumulative evidence anchor
immediately after every node landing. The anchor is a living record: add the
node's commit, gate verdict and quantitative measure, artifacts, tests, and any
negative finding while the evidence is fresh.

**Cumulative evidence file** — `docs/evidence/archive/<slug>-landed.html`:
- Links to `/_shared/foundation.css` and `/_shared/dashboard.css`
- Uses `reckon-type=evidence`; execution evidence never masquerades as a plan
  and never lives under `docs/plans/archive/`
- **`<meta name="plan-evidence-for" content="<slug>">` is MANDATORY** — the
  plan -> generated-evidence back-link. Without it the graph records how
  research informs plans but never which evidence a plan produced, and result
  provenance silently vanishes. Add `plan-verifies` (`slug#section`) when the
  record verifies a specific section, and `plan-informs` ONLY for plans the
  record additionally feeds.
- Stable anchored sections for material outcomes; combine coupled sections
  from the same implementation/test wave rather than restating them
- Compact outcomes table only when it is denser than prose; no status-card
  chrome or one-line documents
- **Figures only where they add understanding (mandate 2026-06-03)**: embed
  result graphics under `docs/figures/<topic>/` with project-absolute `src` when
  spatial, plotted, geometric, topological, or sequential relationships are
  clearer visually. There is no image quota. Worker prompts for doc-producing
  tasks MUST carry this representation-selection rule, not a demand to produce
  an image.
- **Minimal ink is binding.** Follow `reckon-create` hard-rule 8. A graphic must
  communicate more clearly than a short table. Remove outer frames,
  backgrounds, card grids, repeated boxes/pills, decorative colour, duplicate
  headings, and legends that direct labels can replace. Apply the erase test:
  any mark whose removal loses no information must go.
- **Never imagify a table.** Rows/columns of labels, values, verdicts, and short
  explanations belong in a semantic HTML `<table>`, never SVG/PNG/canvas.
  Predominantly tabular-text images fail review even when visually minimal:
  remove the image and keep the selectable, searchable, responsive HTML.

### 6b. Collapse-on-landing — MANDATORY

**When a section ships, IMMEDIATELY collapse it in the evergreen.** Replace the section body with a 2–4 line landed-summary card. Do not accumulate shipped sections.

```html
<section id="s2" class="section-landed">
  <header>
    <span class="badge badge-shipped">✓ landed 2026-06-24</span>
    <h2>§2 — Data prep pipeline</h2>
  </header>
  <p class="landed-summary">
    Built <code>src/data_prep.py</code>; pipeline smoke-test green.
    Encoded 11,237 shots in 3h12m; eval MAE 0.04 — passing.
    Full record: <a href="/<project>/evidence/archive/<slug>-landed#s2">§2 landed</a>
    (commit <code>abc1234</code>).
  </p>
</section>
```

**Rules for the landed summary:**
- 2-4 lines max: what was built (past tense), the **quantitative result** (numbers, verdict), artifact paths, link + SHAs
- A summary that omits the result is incomplete — "landed §2" is not a summary
- Section header gets `✓ landed YYYY-MM-DD` badge (`.badge-shipped`)
- Original prose is retained under the matching anchor in the cumulative
  evidence HTML — gone from evergreen
- **Author as HTML, never markdown**

### 7. Update plan state — in the SAME BEAT as EACH node promotion

```python
# Immediately after reckon crew complete, update this node atomically.
state = read_plan(
    resource={"project": "<project>", "type": "plan", "id": "<slug>"},
    view="raw",
)

landing_detail = (
    "<node> landed — commit <sha>; gate <gate-name> <verdict>; "
    "measure <quantitative-result>; artifacts <paths-or-none>"
)

edit_plan(
  project="<project>",
  slug="<slug>",
  ops=[
    {"op": "set", "path": "impl",
     "value": <completed_executable_nodes> / <total_executable_nodes>},
    {"op": "set", "path": "commits",
     "value": <existing_commits_with_sha_appended_once>},
    {"op": "set", "path": "artifacts",
     "value": <existing_artifacts_with_node_artifacts_appended_once>},
    {"op": "append", "target": "comments", "section": "<section-id>",
     "item": {"id": "c-<timestamp>", "who": "reckon-ship",
              "when": "<iso-now>", "body": landing_detail}}
  ],
  expected_version=state["version"]
)
```

That single state write carries the commit and gate measure alongside `impl`;
a moved percentage is never the only new information. Preserve the manifest's
exact measure and artifact paths rather than replacing them with “passed”. The
cumulative evidence HTML receives the fuller record in the same landing beat.

**`impl` calculation:**
- Set `impl = (count of completed executable nodes) / (count of total executable nodes)`
- Count the whole selected plan, including nodes in partially landed sections;
  the orchestrator owns this denominator because it owns the complete DAG
- Monotonic — only ever increases
- Set it on EVERY node landing, not just the final node in a section

Note: `impl` is a settable scalar — the server does NOT compute it automatically. You MUST set it.

If the node also closes its section, include the driving-followup resolution in
the same `edit_plan` call and then collapse the section as §6b requires. If it
does not close the section, leave that followup open: a node landing advances the
ledger without pretending the larger section is finished.

**Same-plan follow-on work becomes a section, never a followup.** Before setting
a terminal status, add discovered work that belongs to this plan to the evergreen
as a concrete section, add its executable nodes to the DAG, and keep the plan
active. A followup is reserved for work owned by a different plan, whose own
lifecycle keeps it visible to `roadmap`.

### 7b. Continuation closes at THREE altitudes, not one

**Work never ends without naming what comes next** — and the chain has three ends,
not one. Close all three; a chain that closes only at plan level leaves the other
two dangling.

| Altitude | What it owes | How |
|---|---|---|
| **Worker** | candidate follow-ons it was fenced out of | classify the manifest's `follow_ons`: same-plan work becomes a section; different-plan work routes to its owning plan |
| **Plan landing** | visible continuation, or an explicit end | keep same-plan sections active; use a one-line followup only for another plan, or resolve with `done — no followup` |
| **Sprint close** | the sprints this one lets us start | `roadmap(project)` → each sprint row's `feeds_sprints` and `unblocks`, derived from the dependency graph |

A worker's out-of-scope discovery has nowhere to go but prose, where it is lost.
Classify every `follow_ons` entry before writing it. Never feed same-plan work
blindly into `crew.followup_ops_from_manifest`: author it as a section and add
its nodes to the live DAG. Only cross-plan work may become a followup invocation,
pointing at the plan whose lifecycle owns and surfaces it.

**The plan altitude is enforced at the write boundary.** A writeback that lands a
plan — one that resolves a followup or sets a terminal status — is **refused**
unless an open followup still carries the chain or a resolved outcome records in
words that the chain closes (`"done — no followup"`). Do not work around the
refusal: it is telling you the session was about to end silently.

Report the sprint altitude at close from `feeds_sprints` rather than from memory:
it is derived, so it cannot go stale the way a written list would, and a sprint
that feeds nothing says so instead of staying silent.

### 7c. Followup drain — re-triage after every landing beat until dry

**A followup generated during execution is triage input for the current run,
not a handoff by default.** After every landing beat, collect the selected plan's
open followups and the landed manifest's `follow_ons` entries into one
triage queue. Manifest `follow_ons` enter the same triage loop as open plan
followups; their origin changes the evidence trail, not their disposal.

Folding is the default. For each queue entry, either fold its executable work
into the current orchestration — same-plan work becomes an evergreen section and
DAG nodes — or leave it open under exactly one recorded exemption:

- **`authority-required`** — it requires authority the orchestrator does not
  hold: spend, an outward-facing effect, or an irreversible action;
- **`dissent-reopen`** — it asks to reopen a locked decision and therefore
  stays in the dissent flow;
- **`foreign-owner`** — the work belongs to a different plan or repository,
  whose own lifecycle must surface it.

An exempt open followup must record which exemption it claims and the concrete
authority, decision, owning plan, or repository that makes the exemption true.
Do not invent a fourth category such as inconvenience, worker capacity, or
ordinary unfinished work.

After folding and executing the added nodes, re-read open followups before
testing for completion: landing that work may have generated more. Re-triage
after every landing beat and terminate only when a complete pass finds nothing
foldable. A fixed pass count, the end of the original DAG, or an empty
`follow_ons` field from one manifest is not the termination condition.

**Terminal status is gated on this drain.** Do not set `status` to `shipped` or
`done` while any foldable followup is open. Exempt followups may remain open only
with the recorded exemption above; their presence is an explicit authority,
dissent, or ownership boundary rather than forgotten executable work.

### 8. Final validation — eat the dog food

Before declaring the overall plan done:

```python
state = read_plan(
    resource={"project": project, "type": "plan", "id": slug},
    view="raw",
)
# Verify:
assert state["data"]["status"] in ("shipped", "done")   # or "active" if more sections remain
assert state["data"]["impl"] == expected_fraction         # set correctly
# All shipped sections are collapsed in the HTML
# Driving followup is resolved
# A next followup or "done — no followup" outcome is present
# No foldable followup remains open; each exempt open followup records its exemption
# version has incremented
```

For a sprint target, also verify every sprint item is done or explicitly blocked,
all integrated worker commits are reachable from the primary branch, the
sprint summary links its plan/evidence outcomes, and no session worktree
remains. Close the sprint only when all executable nodes are complete.

Re-run `roadmap(project, sprint=<id>)` at closure. Record both lifecycle and
stored implementation completion; require an empty ready set and no error-level
wiring findings before setting the sprint done.

```bash
# Validate HTML integrity
uv run --project ~/Code/reckon reckon audit-doc docs/plans/<slug>.html
# Must report no ERRORs before committing
```

The coordinator runs this itself in either mode. It validates a document the
coordinator just authored — its own state write, not a worker's product — which
is why it is not a delegated node. Product tests are, and remain, worker nodes.

Commit:
```bash
git add docs/plans/<slug>.html docs/evidence/archive/<slug>-landed.html
git commit -m "docs: record verified implementation outcome"
git pull --no-rebase origin <branch>
git push origin <branch>
```

### 9. Surface follow-on work to the user — MANDATORY final-report format

Followup ids (`f-<...>`) are internal plan-state keys — NEVER the primary way
follow-on work is presented to the user. Every session that ends with open
follow-on work MUST close its final report with a **"Next up"** block that
names each follow-on by PLAN + SECTION/RUNG (the human handles), and gives a
fenced, paste-ready prompt so switching to a fresh session is seamless:

````markdown
**Next up** — paste into a fresh session:

```
/reckon-ship <slug> §<N>
/reckon-ship <project>:<sprint-id>
```
````

Rules:
- One fenced prompt per advised follow-on; if several follow-ons are advised
  for one session, stack them in ONE fence in execution order.
- The fenced line is exactly the slash invocation the next session needs. The
  plan owns all guidance, so never append a parenthetical brief or pasted wall.
- Mention the followup id at most once, in passing (e.g. "tracked as
  f-tps-03"), and always AFTER the plan/section name — the id is for plan
  audits, the name is for humans.

## §05 session handoff

> **Canonical §05 contract: `reckon-edit` SKILL.md.** Keep this copy in sync.

Every stored followup prompt and user-facing handoff is one line:

```
/reckon-ship <slug> [§N]
```

The live plan owns all semantic guidance. Internal worker dispatches reference
that plan and add only the runtime safety fields required by
`references/sprint-orchestration.md`; they do not copy the plan into the
handoff.

## Delegation, runtime routing, integration, and cleanup

`references/worker-protocol.md` records everything true of a worker regardless of
backend — the seven-property contract, the four fences, the manifest shape, the
recovery ladder and the escape hatch. The engine injects that content; read the
reference only when hand-composing a delegation Reckon did not prepare.
(`references/worker-backends.md` is maintainer documentation of the translation
internals; an orchestrator never needs it.)

Every node is delegated on both targets. The skill carries the fixed session
contract; use `references/sprint-orchestration.md` only when hand-composing. It
expands on:

- prompt-owned runtime model, effort, and concurrency routing;
- skill and reasoning-effort selection;
- detached worktree creation and worker prompt rules;
- orchestrator-owned merge/conflict handling;
- research-before and evidence-after gates;
- reachability checks and mandatory worktree cleanup.

Use `scripts/worktree_fleet.py` for deterministic worktree creation, inspection,
and cleanup. Workers never mutate shared Reckon state; the orchestrator records
followups, evidence, plan progress, sprint item outcomes, and sprint closure
after integration.

## Cross-references

- `references/worker-protocol.md` — the task contract, fences, manifest, escape hatch.
- `references/worker-backends.md` — maintainer notes on launch translation (not agent-facing).
- `references/orchestrator-harness/<harness>.md` — what the HOST harness lets the
  orchestrator do: background dispatch, wake on completion, self-scheduling a
  held wave's resumption, and whether it can see its own budget. One file per
  host; read the one you are running inside.
- `reckon-edit/SKILL.md` — how the evergreen gets its landed subsection; edit_plan op reference.
- `reckon-create/SKILL.md` — first-time plan scaffolding and §05 invocation.
- `reckon-status/SKILL.md` — read-only inspection before deciding what to ship.
- `reckon-roadmap/SKILL.md` — canonical pending-work graph, critical paths, and wiring diagnostics.
- `~/Code/reckon/PLAN-FORMAT.md` — canonical format (semantic HTML elements, schema contract, endpoints).
- `docs/_shared/plan.schema.json` — published JSON Schema.
