---
name: reckon-build
description: >-
  Execute a complete Reckon plan, or coordinate an entire sprint, without doing
  implementation inline. Resolves a plan slug with an optional section,
  `/reckon-build S1`, a project-qualified sprint id, a bare graph handle, and
  `graph:<handle>`. All targets are strictly
  coordinator-only, a one-node plan included: build the execution DAG, delegate
  every implementation, investigation, test, pipeline, and repair node through
  isolated worktrees by default, audit and integrate worker commits, record
  outcomes continuously, and clean up worktrees. Trigger verbs: "implement /
  execute / ship / land / deliver the sprint / run the sprint / /reckon-build".
  Requests to use local workers, local agents, or local dispatch add `--local`
  to every dispatch, selecting the backend named by `local_backend`.
  For editing plan text use reckon-edit; for defining or rebalancing sprint
  state use reckon-sprint.
allowed-tools: Read Write Edit Bash(*) Grep Agent mcp__reckon__read_plan mcp__reckon__edit_plan mcp__reckon__roadmap mcp__reckon__audit mcp__reckon__crew
---

# reckon-build — execute a complete plan or sprint and record outcomes

## Critical behaviour: resolve the target, then finish its executable scope

There are three execution targets:

- **Single-plan target:** `/reckon-build <slug>` delivers the entire plan;
  `/reckon-build <slug> §N` delivers only the named section.
- **Sprint target:** `/reckon-build S1` executes the current project's sprint;
  `/reckon-build <project>:S1` selects a project explicitly. It reads every
  sprint plan, transitive dependencies, linked research, and prior evidence,
  then coordinates a rolling queue of ready nodes. Use `plan:<slug>` or `sprint:<id>`
  only to disambiguate unusual identifiers.
- **Graph target:** `/reckon-build <handle>` resolves the one endpoint plan
  carrying that handle and executes its complete transitive dependency closure
  across registered project mounts. Handles match
  `[A-Za-z0-9][A-Za-z0-9._-]*`. Only the handle is authored on the endpoint;
  membership, shipped-of-total, critical path, and average width are
  derived live. `/reckon-build graph:<handle>` is the unambiguous long form.

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

Use `roadmap(project, sprint=<id>)` for a sprint and
`roadmap(project="graph:<handle>", view="raw")` for a graph target as the
canonical plan-level graph. Do not
rebuild plan dependency order from repeated discovery calls. Add section-level
and file-conflict edges only after the tool returns no error-level wiring
findings.

### All targets are coordinator-only

**One contract, all targets, a one-node plan included.** Whichever target resolved,
preserve coordinator context for orchestration. The coordinator may resolve/read
state, checkpoint the DAG/scopes, create worktrees, dispatch or message workers,
audit evidence, integrate/push, write state, clean, and report.

The coordinator MUST NOT implement, investigate implementation details beyond
scoping/review, edit product/source/test files, execute tests or operational
pipelines, or repair worker code. Every implementation, investigation, test
execution, operational pipeline run, and corrective repair is a worker node,
even when only one item is ready and a worker could take it at once. On failure,
redispatch a corrective worker. The detailed contract, manifest, and checkpoint
discipline live in `references/sprint-orchestration.md`.

**A small plan is not an exception.** Even one node needs worktree isolation,
independent review, a manifest, and a ledger record; node count changes fleet
size, never whether work is delegated. Inline fallback is a reported exception
only when no capable worker backend exists. Member scarcity never qualifies —
register one. State the exception and its context cost before implementing, and
prefer pausing the node.

### Continuity — who receives the next piece of work

A session continues the task it began. Only resume, redispatch, repair, or re-review of that same run may reuse its session; every new node, widened scope, or new task starts fresh.

For the reuse rationale and stopping triage, read [conditional guidance: Continuity, stopping, and prerequisite diagnosis](references/conditional-guidance.md#continuity-stopping-and-prerequisite-diagnosis) only when evaluating an exception.


## Fast path

```text
resolve target
├─ plan → roadmap + full plan → classify sections → delegate in dependency order
├─ sprint → roadmap(view=summary) + PENDING plans + their deps/evidence → enrich DAG → ready queue
└─ graph → graph roadmap + every closure plan/evidence → run derived ready queue
     ↓
read task requirements + apply explicit runtime routing + applicable skill
→ audit plan currency against the code + dispatch the prior-art scout (reuse map)
→ check every node against the eight-property contract (§3b)
→ arm ONE bare `crew follow --project P --session S` as the session's wake-up — never a shell, never a poll loop (rule 18)
→ reckon crew preflight — a spent backend holds its nodes, the rest still run
→ reckon crew dispatch each ready node — branch only on the returned launch kind
→ emit the dispatch summary, naming the gate that closes the wave
→ untrust-check, then READ each worker's diff by anomaly (§5b) → orchestrator merges
→ reckon crew complete each run — the record becomes committed evidence
→ reckon crew verify-gate — re-run a promoted run's own gate at the merged head
→ read and merge the worker-authored record, may edit or append (§5 step 7), then write impl
→ emit the completion summary, WHY carrying the gate evidence
→ re-triage open followups + manifest follow_ons; fold them into the DAG until dry
→ write the drain ledger; foldable-remaining and unreconciled-runs must read 0
→ record any deliberate live-pointer remainder through reckon crew drain
→ record plan/evidence/sprint outcomes + continuation at all three altitudes
→ prove commits reachable → remove worktrees → close sprint when complete
```

Full detail below.

## When to invoke

- "implement / execute / ship X" / "land items from X" / "do the work in X plan"
- `/reckon-build <slug>` — implements the WHOLE PLAN
- `/reckon-build <slug> [§N]` — implements only the named section
- `/reckon-build S1` — executes sprint `S1` in the current project
- `/reckon-build <project>:S1` — executes a sprint in an explicit project
- `/reckon-build <handle>` — executes the derived cross-project closure when a
  live endpoint uniquely claims the token as its graph handle
- `/reckon-build graph:<handle>` — the unambiguous long form for that closure
- Reading a §05 followup whose `recommends_skill` is `/reckon-build`
- "use local workers / use local agents / dispatch locally" — these are routing
  instructions that add `--local` to every `reckon crew dispatch`. The flag
  selects the backend named by `local_backend` and refuses when it is unset.
  The task itself
  may be any implementable plan, investigation, test, or documentation node —
  "use local workers" selects the *backend*, not the task scope.

**Dual-role:** invoked by human or orchestrator AND records one-line §05 session handoffs.

If the user wants to *write* the plan → `reckon-edit`. Plan doesn't exist → `reckon-create` first.

## The model — the plan HTML is the document AND the store

**The plan HTML is the source of truth.** Read it first — ALL of it. All targets
delegate what it describes and coordinate the outcomes; the modes differ in the
scope they resolve, never in who does the work. The HTML documents the work;
the `data-reckon` sections carry
structured state (decisions, followups). Do not implement items marked
"deferred", "post-v1", or behind an unmet trigger.

**Write path:** use `edit_plan` to record outcomes atomically:
1. `read_plan(resource={project,type:"plan",id:slug}, view="raw")` → get `version`.
2. `edit_plan(…, ops=[set status/impl + resolve driving followup + append next followup], expected_version=…)`.
3. On 412 conflict: re-read + retry.

## Reading state: ask the tool, and ask it for a view

**Every routine question about plans, runs, sprints or budgets is an MCP read.
Reaching for bash is almost always a sign the call was made wrongly, not that
the surface is missing.**

- **Always pass `view`.** A single-project `roadmap` without one returns the
  lossless legacy report, which at a real project's size cannot be returned at
  all. `summary` answers nearly every question; ask for `raw` deliberately.
- `crew(view="flight"|"budget"|"drain"|"live")` answers what `reckon flight`
  and `reckon crew preflight`/`drain`/`list` answer. Prefer the tool.
- `read_plan(..., view="summary")` before `view="raw"`; raw is for editing, not
  for looking.

**When a shell command genuinely is the right tool, name its target.** A form
that leaves the working directory or the traversal set unresolvable to the
safety classifier turns an ordinary read into a prompt for the operator, because
the classifier reasons about the command text and cannot rule out a denied path
it cannot see excluded:

- `git -C /path/to/repo log …` — never `cd /path/to/repo && git log …`
- `grep -rn "<pat>" src/ tests/` — never `grep -rn "<pat>" .` from a repo root,
  which walks `.env`. **`--include=*.py` does not help**: it constrains what is
  reported, not what the classifier must prove about the walk.

Naming subtrees also skips `.venv`, so it is faster as well as quieter. The
remedy is always the command form, never a wider permission. Full rule and the
measured instances: `~/.agents/AGENTS.md`, *Name The Target*.

## Hard rules

1. **Read the full PENDING scope before ANY dispatch — not the whole sprint.** On a
   single-plan target, read the complete plan. On a sprint target, read the sprint
   index, then `roadmap(project, sprint=<id>, view="summary")`, and then **every plan
   in `pending_work`** together with each of their transitive dependencies, linked
   research and prior evidence. On a graph target, read every plan in the returned
   derived closure and its linked evidence.

   **A completed plan is a record and is not re-read on a relaunch.** `roadmap`
   already excludes a plan at `impl` 1.0 **or** status `shipped`/`done` from
   `pending_work`, `ready_now`, `critical_path` and every open path — that is §7a-bis's
   mechanism, and reading those plans in full is the single largest re-litigation cost
   of resuming an in-work sprint in a fresh session. Read a completed plan only when a
   pending plan **depends** on it, and then only for the contract it provides — its
   landed-summary and locked decisions, not its run history.

   **Trust `pending_work` rather than recounting.** A sprint's `completed` figure
   counts membership from the project index and can differ from a plan-metadata
   count; `pending_work` is the set that governs dispatch.
2. **Full plan by default.** `/reckon-build <slug>` without a section flag means ALL implementable sections. Never implement one section and stop unless there is a hard blocker.
3. **Whole sprint by default.** `/reckon-build S1` means every executable item in the sprint plus actionable same-project prerequisites.
4. **Whole graph by default.** `/reckon-build <handle>` or its unambiguous long
   form `/reckon-build graph:<handle>` means every member
   returned by the live derived closure, including dependencies in other mounted
   repositories. A missing handle or an open non-deferred decision is a refusal.
   Shipping explicitly overrides the schedule window: report
   `schedule_override.deferred` and its members before dispatch rather than
   silently treating them as schedule-ready.
5. **Coordinators delegate every executable node, on all targets.** This includes a plan holding exactly one node, investigation, test execution, operational pipelines, and corrective repair. Inline is the reported exception of §All targets are coordinator-only, never the default for small work.
6. **Verify every worker by reading what it produced, not by trusting its gate.** Run the cheap untrust checks of §5b first — manifest mtime against dispatch time, `commits:` against the worktree's `rev-parse HEAD`, a clean `status --porcelain`, `git show --stat <sha>` against declared scope, and the gate log actually existing and agreeing with the `tests:` line. Then read the diff by anomaly rather than sequentially, at a depth set by blast radius × mechanicalness. A passing gate the implementing worker wrote and ran against its own diff measures internal consistency, not correctness. Test execution is itself a worker node.
7. **Scope allocation precedes dispatch.** Use isolated worktrees by default; list each worker's exclusive write paths before sending a prompt. No two workers share a file.
8. **The portable dispatch contract is mandatory.** `reckon crew dispatch`
   composes it. Read and embed the reference contract only when hand-composing a
   delegation Reckon did not prepare.
9. **Update the plan at every node landing.** Immediately after EACH
   `reckon crew complete`, the orchestrator merges the worker's committed record —
   reading it, editing or appending to it as needed — then calls `edit_plan` once
   with the advanced `impl` and the shared index additions. Nothing else may be
   promoted or merged before this write.
   Dispatch of an unrelated ready node may continue; it does not wait on this
   plan-write beat. Collapse the evergreen section only when its final node lands;
   never wait for section closure to record earlier nodes.
10. **One cumulative evidence record and a followup are required.** Default to
   `docs/evidence/archive/<slug>-landed.html`, carrying
   `reckon-type=evidence` and `plan-evidence-for=<slug>`, with one stable anchor
   per material result. Update that record after each landing. Do not create a
   file per section, commit, test wave, or one-line outcome. Create another
   evidence resource only when it is a materially independent artifact useful
   on its own.
11. **Collapse the evergreen when a section ships.** Replace the section body
    with a 2-4 line landed-summary + link to the matching anchor in the
    cumulative evidence HTML.
12. **No plan-state drift.** Plan and sprint state must reflect reality at the end of every turn.
13. **The sprint coordinator owns only coordination, integration, and shared state.** Workers commit implementation and verification work in detached worktrees; they do not merge, push the primary branch, or mutate the shared index/plan state.
14. **Cleanup is mandatory and conservative.** Remove a worktree only after it is clean and its commit is reachable from the integrated primary branch. Never force-remove unmerged or dirty worktrees.
15. **Do not execute a malformed graph.** Invalid, dangling, non-executable,
    inactive, contradictory, cyclic, sprint-order, or membership findings are
    wiring repairs, not scientific blockers and not override prompts.
16. **Drain followups and live run pointers before closure, and sign both.** Re-triage open
    followups after every landing beat, route that node's manifest `follow_ons`
    through the same loop, and repeat until a complete pass finds nothing foldable.
    Write the §7c drain ledger before any closing report: every row disposed as
    `folded` (with its node id), `authority-required`, `dissent-reopen`,
    `foreign-owner`, or `context-exhausted` (with its figure). **A session does not
    end while `foldable-remaining` or `unreconciled-runs` is nonzero** — see
    the closure fence, §4d. A deliberate pointer remainder must be recorded by
    `reckon crew drain --project <project> --leave <run-id>=<disposition>` using
    only `handed-off` or `still-working`; the latter expires when the run turns
    terminal. Never
    set a plan to `shipped` or `done` while a foldable followup is open.
17. **No implementation node before the reuse map exists.** Every substantive
    implementation wave is preceded by a prior-art scout (§1b) whose reuse map
    the nodes cite; a node that authors new machinery states why each named
    reuse candidate fails. Trivial mechanical edits are exempt; "the scout
    would slow us down" is not.
18. **The follower is the wake-up; a shell is not.** Arm exactly one
    `reckon crew follow --project P --session S` per session, bare, through the
    harness's per-line notification primitive (§One producer, and
    `references/orchestrator-harness/<harness>.md`). It delivers every
    transition to this session as it happens. With it armed you do not need a
    shell to find out what your fleet is doing, and **a shell must never be used
    as a wake-up**: no `sleep`, no poll loop, no backgrounded command whose
    purpose is to wait, and nothing re-run on a timer to see whether a run
    finished. A command that reports only on exit delivers nothing here, because
    what you are waiting on does not exit — the follower does not exit either,
    which is why it is armed as a stream and not awaited.

    Three costs make this binding rather than stylistic. A poll loop burns a
    full turn per check and reads a pointer the follower already pushed. It
    reads *stale* state between checks, so a run that finished is unnoticed
    until the next tick, which is the measured cause of finished runs sitting
    for hours. And every shell started to wait is a process on a shared login
    node, aimed at nothing.

    What a shell IS for: a one-shot read that answers a question the follower
    does not carry — a diff, a git check, a gate log. Read fleet state through
    `crew(project, view="live")` at turn time rather than by shelling out, and
    let the follower decide when that turn happens.

## §Prerequisite blocking — STOP and ask for authorization

On a single-plan target, an unshipped prerequisite is a hard stop unless the user authorises implementation or override. On a sprint target, actionable same-project prerequisites join the DAG; stop for cross-project, unavailable, abandoned, or authority-expanding prerequisites.

Gate on the computed `deps` list rather than assuming a bare reference is local. Repair error-level `roadmap.wiring_findings` before classifying a prerequisite, and never ask the user to override a relationship Reckon identifies as malformed.

For relationship diagnosis and the exact plan-mode authorization prompt, read [conditional guidance: Continuity, stopping, and prerequisite diagnosis](references/conditional-guidance.md#continuity-stopping-and-prerequisite-diagnosis) only when the prerequisite stop applies.


## Workflow

### 0. Resolve plan, sprint, or graph

The resolution mechanics for a plan, sprint, or graph target — how a bare
token is matched against exact sprint ids and the handle grammar, and when a
target resolves as a graph rather than a plan — are in
`references/sprint-orchestration.md` §1. Call `roadmap(project)` first and
match the argument against exact sprint ids before any other treatment; a
token no live endpoint claims as its graph handle remains a single-plan target.

### 1. Plan pre-flight — read the FULL plan

**This step is NON-NEGOTIABLE. Do not skip it. Do not begin implementation until it is complete.**

Call `roadmap(project)` first. Require the target to be in `ready_now`, or
follow its earliest local prerequisite from the returned critical/open path.
Resolve every error-level wiring finding before implementation.

```python
state = read_plan(resource={"project": "<project>", "type": "plan",
                            "id": "<slug>"}, view="raw")
contract = read_plan(resource={"project": "<project>", "type": "plan",
                               "id": "<slug>"}, view="schema")
```

The MCP payload holds parsed state; the prose lives in the file. Read the
COMPLETE HTML from disk (`cat docs/plans/<slug>.html`) and do not proceed until
you have read every `<h2>` section, all decision and followup items (locked,
open, and resolved), the `plan-depends-on` meta tag, and any `Trigger:`
subsections or deferral markers.

### 1b. Currency audit and prior-art reconnaissance — MANDATORY

Plans go stale, and agents rebuild what already exists. Audit plan currency
against the code before cutting nodes, and dispatch a prior-art scout whose
single deliverable is a REUSE MAP that nodes then cite. The scout is an
ordinary investigate-role node — never a harness-native background agent and
never inline. The full mechanics of the scout and the citation rule are in
`references/sprint-orchestration.md` §Prior-art reconnaissance.

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
  Implementable: §2 (3 items), §3 (2 items)   Deferred: §5 — post-v1
  Already done:  §1 — commit abc1234           Prerequisites: CLEAR
Dispatch plan: §2 fleet of 3 → §3 fleet of 2 (§3 depends on §2 output)
```

Persist that DAG-build classification before dispatch with one version-safe
`edit_plan` write to `section_declarations`, mapping every authored section id
to `implementable`, `deferred`, or `done`. Re-read the plan and require the
field to match the complete audit. This declaration establishes the executable
denominator; omitting it means the remainder is unknown, never zero.

### 3. Scope allocation

List **exclusive write paths** per item. If two items share a file, serialise them or split it (`test_a.py` / `test_b.py`).

**Never dispatch two workers that write the same file.**

### 3b. Pre-dispatch checklist — a node is well-formed BEFORE it is sent

**A worker that thrashes is almost always executing a malformed task.** Check
well-formedness before dispatch, not after; that leaves the escape hatch handling
only the genuine residual. A node that fails is reshaped or split — never
dispatched in the hope that the worker will work it out.

All eight must hold:

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
- [ ] **Specification level declared** — `--spec-level` is one of `exact`,
      `guided`, or `open`; undeclared work is refused because it cannot enter a
      calibration slice.

`reckon crew dispatch` enforces the same eight and exits 2 naming every failing
property. Check every node before it enters the ready queue:

```bash
reckon crew dispatch --project P --plan L --section §N --role implement \
  --node <id> --goal "<one deliverable>" --done-when "<measure>" \
  --write-path <path> --session <session> --dry-run
```

Worktree creation needs nothing installed in the dispatched repository: the
fleet script is resolved from the running reckon. This matters most when the
write repository is named separately from the plan's with `--repo`, which is
otherwise the first place a per-repository prerequisite would bite.

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
| `unreconciled-runs` | 6 | Run each reported reconciliation command, or use `--allow-unreconciled-runs` when the backlog must deliberately remain; the waiver is recorded on the new run. |
| `scope-conflict` | 7 | Re-plan after the reported owning run releases its containing or contained path claim. |
| `watcher-required` | 8 | No watcher this session can rely on — either the project has no live watcher process or this session has no delivering follower, and the refusal names which. Repair the first with `reckon crew watch --ensure --project P` and the second by arming the `attach_line`. Use `--no-watch` only for a synchronous one-off whose waiver belongs on the run. |
| `member-in-flight` | 9 | Wait for the named run to reach a terminal phase, or dispatch to a different roster member. |

Every refusal answers with a JSON document on stdout carrying `error` and
`detail`, including the ones with no code of their own, which exit 1 as
`dispatch-refused`. Read that document rather than inferring a refusal from an
empty stream — several dispatches chained in one shell command leave only the
last exit status behind.

Three node-contract refusals are easy to mistake for infrastructure failures:

- A dirty plan HTML file cannot supply base-revision authority. Commit the plan
  before dispatching; `plan-unavailable` names the missing or changed section.
  **This beat is the design, not an obstacle in front of it.** The plan is the
  passing surface: the worker reads its section from its own worktree at the base
  revision, so anything a worker must know has to be committed there rather than
  copied into the handoff. Findings, constraints and evidence inputs discovered
  mid-session therefore go *into the plan first* — record, commit, dispatch, in
  that order. That is what makes them readable by the worker, by a reviewer, by
  `roadmap`, and by the next session, instead of dying with one prompt. There is
  deliberately no flag for passing prose to a worker: a second store is a second
  stale source of truth, and the dispatch flags carry only what genuinely cannot
  live in a plan — worktree, scope, manifest path, budgets, session, and routing
  overrides. Large inputs travel by reference: put the artifact at a path and
  name the path in the node's evidence inputs.
- A goal containing `;` is not one deliverable. Rewrite it as one outcome; use
  the DAG for sequential work. Action-bearing `and`, `&`, or `plus` clauses are
  refused for the same reason.
- Every node needs at least one `--write-path`. For verification, cleanup, or
  other work with no tracked repository output, name the on-disk report, log, or
  artifact path that constitutes its exclusive delivery scope.

The engine supplies the full contract, manifest shape, and escape hatch. Read
`references/worker-protocol.md` only when hand-composing a delegation Reckon did
not prepare.

### 3c. Locally served worker routing

When the invoking phrase includes "use local workers", "use local agents", or
"dispatch locally", the coordinator adds `--local` to every
`reckon crew dispatch` call. The flag resolves `local_backend` as that
dispatch's default and refuses when it is unset; role overlays still apply.

**The local lane is a routing choice, not only a flag.** A coordinator may
select it unprompted when the node's declared level is `exact`, when the
metered lanes are constrained, or when the node needs no decision. It costs
no metered quota. Context-fit refusal now rejects a node exceeding the lane's
window before a worktree exists, naming the estimate, the window and the
shortfall, rather than the node dying mid-run. The flag contract above is
unchanged: a request to use local workers still selects that same configured
backend.

The wrapper supplies the base URL, model tiers, and credential for the locally
served endpoint. Its backend alias reuses the existing pass-through dialect
because the wrapper forwards its arguments unchanged.

No other part of the coordinator workflow changes: the node still gets a
worktree, a manifest, a gate, and a ledger record. Only the backend selection
differs. The local backend also supports session reuse and token tracking (no
rate-limit headroom since it is a local server).

Check that the configured local backend resolves before dispatching:
```bash
reckon flight --project P --pretty
```

#### The done-when is the specification, and on this lane that is literal

**A worker is graded on the measure.** Every goal claim must emit evidence in the done-when; state removals as a negative measurement; and require the declared mutation, named failure, and recorded log in that measure. Keep nodes narrow enough that their evidence and context are independently reviewable.

Treat a published lane capacity as advisory. Inspect its derivation and observed pressure before turning its headroom into a hold.

For measure reviews, removal checks, negative-control execution, and capacity rationale, read [conditional guidance: Local-lane specification and capacity rationale](references/conditional-guidance.md#local-lane-specification-and-capacity-rationale) only when designing a local-lane node.


### 4. Dispatch workers

Pre-flight the routing surface once per session with `reckon flight --project P`:
it reports the resolved config, which layer supplied each value, and which
backends are actually available.

Dispatch ensures one live `reckon crew watch --project P` producer before it
creates a worktree, reusing a kernel-backed seat or starting one detached, so
ending the dispatching process does not orphan it. **That producer is not your
wake-up.** A seat is project-global and delivery is session-local, so a
CLI-launching dispatch is refused until this session has a live follower of its
own: arm the payload's `attach_line` — `reckon crew follow --project P
--session S` — through the per-line notification primitive named in
`references/orchestrator-harness/<harness>.md`. Anything that reports only on
exit delivers nothing, because the follower does not exit. `--no-watch` is the
explicit exception for a genuinely synchronous one-off; it records the arming
command, the observed watcher liveness and the unattached session on both the
live run and its promoted ledger record.

**One dispatch instruction covers every backend.** State what the node is;
reckon resolves how it runs. Which harness, at what model, effort and sandbox
tier, comes from flight config — so this skill names none of them, and a
per-task deviation is an override on the same call rather than a different call:

```bash
reckon crew dispatch --project P --plan L --section §N --role implement \
  --node <id> --goal "<one deliverable>" --done-when "<numeric measure YOU name>" \
  --write-path <path> \
  --time-budget 25m --session <session> [--member <id>] [--set backends.<name>.effort=high]
```

Peer scopes come from live pointers in the same project and repository. Dispatch
refuses containing or contained path claims before creating a worktree and names
the owning run. `--peer <other-node>=<their-paths>` is optional: use it only to
supplement peers that do not yet have a live pointer, never as the source of the
live claim registry.

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

**A worker's gate ran against the base its worktree branched from, so a contract
that landed after that base never bound it.** The run is legitimately green, the
merge turns the primary branch red, and nothing between the two looked at the
tree that ships. After merging, re-run the run's own gate at the integrated
revision:

```bash
reckon crew verify-gate --project <project> --run <run-id> \
  --checkout-path <the merged tree> [--revision HEAD]
```

It reads the gate command and base verdict from the run's committed ledger row,
re-runs that command against the merged checkout, and patches the full report
back onto the row as `integrated_gate_check` — so the finding outlives the run
directory. It refuses to execute against any tree that is not the integrated
revision, because a run there verifies the wrong tree, and reports `not-run`
rather than `passed` when the command is absent or exceeds its bound. A finding
appears exactly when a gate green at the worker's base is not passed by the tree
about to be pushed; it never manufactures one from an already-red base. A run
that was never promoted is refused, naming `reckon crew complete` as the repair.

`reckon crew observe --run <id>` folds the worker's stream, manifest presence and
process liveness back into its record — phase, captured session id, and whatever
budget signal the backend emitted, which may legitimately read `unknown`. **Never
read an absent budget signal as exhaustion.**

### Reading run state — MCP owns reads, the CLI owns actions

**Use the `crew` MCP tool for fleet reads; use the CLI only for state-changing actions.** Read `live` for current runs, `scopes` for ownership, `drain` for closure, `ledger` or `records` for committed outcomes, `flight` for routing, and `budget` for headroom. Unknown budget evidence is not exhaustion. Let the follower deliver transitions; do not poll with a shell.

```text
crew(project, view="live")      crew(project, view="scopes")
crew(project, view="drain")     crew(project, view="ledger")
crew(project, view="summary")   crew(project, view="flight")
crew(project, view="records")   crew(project, view="budget")
crew(project, view="lanes")
```

The compact CLI inventory is the action surface, not a polling substitute:

```text
reckon crew attach --run <id> --task <task-id>
reckon crew check-manifest --run <id>
reckon crew complete --run <id> --gate <verdict> --commit <sha> --outcome <text> --tests-added <n> --scope-changed
reckon crew directory --project <project> --run <id> --node <node>
reckon crew dispatch --project <project> --plan <slug> --section <section> --role <role> --node <node> --goal <goal> --done-when <measure> --write-path <path> --peer <other-node>=<their-paths> --time-budget <duration> --session <session> --set <override> --dry-run --member <member> --manifest <path> --allow-unreconciled-runs --no-watch
reckon crew discard --run <id>    reckon crew drain --project <project> --leave <id>=<disposition>
reckon crew follow --project <project>    reckon crew gc    reckon crew ledger --project <project> --view <view>
reckon crew list    reckon crew member add    reckon crew member list --project <project>
reckon crew observe --run <id>    reckon crew preflight --project <project> --role <role>
reckon crew placement --ensure --session <session> --project <project>
reckon crew recover    reckon crew redispatch --run <id> --backend <backend> --reason <text>
reckon crew resume --run <id> --advice <text>    reckon crew resume-ready --project <project>
reckon crew shadow    reckon crew stop    reckon crew unwatch --project <project>
reckon crew verify-gate --project <project> --run <id> --checkout-path <path>
reckon crew watch --project <project> --stall-window <duration>
reckon crew widen --run <id> --write-path <path>
reckon flight --project <project>    reckon audit-doc
```

For view-by-view semantics, cross-repository contact, recovery commands, and integrated-gate receipts, read [conditional guidance: Dispatch lifecycle, read views, and integrated gates](references/conditional-guidance.md#dispatch-lifecycle-read-views-and-integrated-gates) only after the core action identifies the needed view.


### One producer for the project, one follower for your session

A producer turns project pointer changes into transitions; a follower delivers this session's transitions. **That producer is not your wake-up.** Arm the payload's `attach_line` before the first dispatch and confirm `session_attached`, not merely producer liveness.

**Exactly one monitor per session.** A session arms and attaches exactly ONE monitor: one `reckon crew follow --project P --session S` follower, one per session. A second follower on the same session is a defect, not redundancy. To watch more than your own runs, name them on the one follower with `--observe-session`; **Never arm a second follower.**

Read `references/orchestrator-harness/<harness>.md` before arming. The follower **produces lines, not an exit**: attach its bare, coloured command through the host's line-delivery primitive. A CLI dispatch without its follower is refused before a worktree exists; use `reckon crew watch --ensure --project P` only when the refusal names a missing producer, and use `--no-watch` only for a synchronous one-off whose waiver reaches the promoted ledger record.

When transitions go quiet, call `crew(project, view="live")`; zero `working` with non-zero `blocked` or `unpromoted`, or a non-terminal in-flight run with no process, is not a completed fleet. Read gate evidence before promotion, then reconcile old terminal pointers before opening more work.

The live classifier reads the manifest's recorded status: `complete` is
`completed_unpromoted`, while `blocked` and `failed` remain terminal evidence.

For observer replacement, project-wide counts, stream flags, colour, and pointer-classification details, read [conditional guidance: Follower mechanics and stalled-fleet recovery](references/conditional-guidance.md#follower-mechanics-and-stalled-fleet-recovery) only while operating or recovering a follower.


### Concurrency — the roster is the whole authority

**There is no slot pool and no numeric worker cap anywhere in Reckon.** The one
binding rule is per-member serialisation: dispatch refuses a member that
already owns a non-terminal live pointer, so the concurrency ceiling is exactly
the number of registered members with no run in flight: **free members are the
only ceiling.** Backend `concurrency:` keys in flight config are retired and
ignored. Never treat
"the pool is loaded" as a constraint, a reason to queue ready independent
work, or a reason to route around the crew system.

**Every dispatch gets a disposable worker, and concurrency comes from the
members a project already has.** Reuse them and register none: `reckon crew
member add` is not a concurrency lever, because a roster member is a named,
reusable identity — a registration made to buy concurrency leaks a session
across tasks and churns the committed `crew.json` under peer sessions.

**Where ready nodes outnumber free members, the coordinator never registers enough
members to meet it** and the wave waits for a member to finish; dispatch
resumes as one does. Then redispatch each member as soon as its finished node
is verified; no dependent node builds on unverified work. Do not wait for the
slowest active node.

### Advisory fleet-size guide

**This table is advisory.** It shapes the active fleet; it never decides whether to delegate. Dependency independence, file scopes, gates, budgets, and runtime limits shape the useful fleet size; none of them is a slot pool.

| Items | Strategy |
|---|---|
| 1 | One worktree worker |
| 2–8 independent | Parallel worktree fleet when workers are available |
| > 8 | Reader fan-out followed by one synthesis/integration owner |
| Cross-cutting / strategic | One highest-capability worker; do not fragment context |

Apply explicit prompt routing; otherwise choose and record the runtime override from the declared specification level. Use `references/effort-routing.md` for effort and `references/lane-routing.md` for named-lane choice, refusal conditions, and selection evidence.


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

- **A hold is not a failure.** Nothing is created or unwound; the nodes stay
  ready. `reckon crew dispatch` exits 3 with a `hold` payload for the same reason.
- **A hold is per-backend.** The pre-flight reports held and clear backends side
  by side; the clear ones dispatch in the same wave.
- **Unknown never holds.** A backend publishing no headroom reads `unknown` and
  the wave opens — absence of a signal is not evidence of exhaustion.
- **A hold is never silent.** Report it on the four axes below with occasion
  `hold`: what is held, why — with the figure — how it recovers, when it lifts.

A fresh dispatch stops at the ceiling *less* `budget.resume_reserve_pct`;
answering a stuck worker may spend the reserve — spending the last of a quota on
a new node leaves nothing to answer a `NEEDS-HELP:` report with.

**Resuming a held wave without a human is a host capability, not a process rule** —
documented per host in `references/orchestrator-harness/<harness>.md`. Where no
such capability exists, report the reset time and stop — degraded, not broken,
never a reason to dispatch into the quota anyway.

### 4d. The closure fence — a session does not end into available work

**Authored here and nowhere else.** §4b and §4c guard doing too much. **This
fence guards stopping — the unguarded move, because stopping produces no error,
no refusal, and no artifact, only a tidy report.** Overrunning a gate or budget
announces itself; under-running a plan announces nothing, which is why stopping
gets the heavier machinery.

Before the closing summary, **refuse to end the session while any queue row is
foldable or any live pointer is unreconciled.** Run the followup drain of §7c,
then call `crew(project, view="drain")`, write both figures into the ledger, and
read it back:

- every open row carries a disposition from the closed set, or the session
  continues;
- a row disposed `folded` with no dispatched node id is not folded — it is a
  stop with paperwork;
- `context-exhausted` is the one disposition that ends a session with foldable
  work outstanding, and it must carry its figure (§7c).
- every live pointer counts as unreconciled unless it carries a valid run
  disposition recorded through `reckon crew drain --project <project> --leave
  <run-id>=<disposition>`;
- the run-disposition set is exactly `handed-off` and `still-working`;
  `still-working` remains valid only while the live classifier reports
  `running`, while a promoted run leaves the count by losing its pointer.

Report the fence on the four axes of §4e with occasion `close`. A session ending
with `foldable-remaining: 0` and `unreconciled-runs: 0` has earned its summary;
one ending with an unexplained nonzero count has not, and the ledger says so.

### 4e. The summary reflex — what, why, how, when

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
```

`WHAT` names nodes and artifacts. `WHY` gives the causal reason this wave runs
now — **and at completion or a hold it carries the figure.** `HOW` carries runtime
and isolation facts only. `WHEN` gives a duration estimate and names the gate that
closes the wave, or the reset that lifts the hold. The discipline forces every
wave report to be quantitative: a wave that cannot state its gate evidence is
visibly incomplete rather than plausibly done. Completion and hold examples:
`references/sprint-orchestration.md` §10.

### 5. Verify every worker — MANDATORY

Verify each finished worker before integration or releasing a dependent node: read its manifest, confirm its commit and clean worktree, compare `git show --stat` with declared scope, open the named gate log, then read the diff by anomaly. Fold final stream state with `reckon crew observe --run <id>` and promote only with `reckon crew complete --run <id> --gate <verdict> --commit <sha>` after the evidence is coherent. The landing is a **review of an authored record, not a transcription of a manifest**. Workers author their own landing record in the same beat, while the orchestrator writes shared state. Never mutate the shared project index, sprint state, or another plan from a worker record. Immediately perform that review and plan write before another promotion; dispatching an unrelated ready node is outside this freeze.

**Dispatch a test worker and audit its compact result manifest** before
releasing a dependent node: the implementing worker's own gate is not
independent evidence. The full integration sequence, including merge order and
the post-merge test dispatch, is `references/sprint-orchestration.md` §7.

A provider refusal or an idle worker without a report is not automatically a failed implementation: inspect its recorded deliverables and resume or request them before redispatching. A genuine incomplete result receives a corrective worker in its original scope; dependent work remains closed.

For the seven cheap checks, review-depth selection, and provider or report recovery, read [conditional guidance: Verification depth and incomplete worker reports](references/conditional-guidance.md#verification-depth-and-incomplete-worker-reports) only when evidence needs diagnosis.


### 5b. Read what the worker produced — the gate is not the evidence

**Authored from measurement, not preference.** A passing gate the implementing worker wrote and ran against its own diff measures internal consistency, not correctness, so every landing is read before integration and release. Run the cheap checks in yield-per-second order before reading any code, then read the diff by anomaly at a depth set by blast radius × mechanicalness.

The full practice — the four measured gate shapes, the seven cheap checks, the anomaly read, the depth rules, why the coordinator names the measure instead of leaving the worker to write its own gate, and when to batch an independent review — lives in `references/worker-verification.md`. Keeping it there is what bounds the fixed session read set.

### 6. Record outcomes — after EACH node

**Do not wait until a section is done.** The cumulative evidence anchor is a
living record: immediately after every node landing, verify the worker's own
anchor came in with its merge and append only what the worker could not state —
the gate verdict and the negative findings. Never transcribe the worker's record
afresh while the evidence is fresh.

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
- **Figures and representation follow `reckon-create` hard-rule 8**, which is
  binding here too: figures only where a spatial/plotted/sequential relationship
  is clearer visually (no image quota), minimal ink with the erase test, and
  never an image of what is naturally an HTML `<table>`. Worker prompts for
  doc-producing tasks MUST carry this representation-selection rule, not a
  demand to produce an image.

### 6b. Collapse-on-landing — MANDATORY

**When a section ships, IMMEDIATELY collapse it in the evergreen.** Replace the section body with a 2–4 line landed-summary card. Do not accumulate shipped sections.

```html
<section id="s2" class="section-landed">
  <header><span class="badge badge-shipped">✓ landed 2026-06-24</span>
    <h2>§2 — Data prep pipeline</h2></header>
  <p class="landed-summary">Built <code>src/data_prep.py</code>; smoke-test
    green. Encoded 11,237 shots in 3h12m; eval MAE 0.04 — passing. Full record:
    <a href="/<project>/evidence/archive/<slug>-landed#s2">§2 landed</a>
    (commit <code>abc1234</code>).</p>
</section>
```

**Rules for the landed summary:** 2-4 lines — what was built (past tense), the
**quantitative result** ("landed §2" without its numbers is not a summary),
artifact paths, evidence link + SHAs; `✓ landed YYYY-MM-DD` badge
(`.badge-shipped`) on the header; original prose moves to the cumulative
evidence anchor; **HTML, never markdown**.

In the same collapse-on-landing beat, reclassify its `section_declarations`
entry to `done`. A section comment records a landed node, not completion: only
`done` lowers its executable remainder.

### 7. Update plan state — in the SAME BEAT as EACH node promotion

The landing beat reviews an authored record: the worker's own section comment
and evidence anchor arrive in its committed plan, which this beat merges. Read
that record and may edit or append to it — never write from the manifest a
record the worker is required to author.

```python
# Immediately after reckon crew complete, update this node atomically.
state = read_plan(
    resource={"project": "<project>", "type": "plan", "id": "<slug>"},
    view="raw",
)

# The worker's record merges in with its commit. Append to its own comment only
# the closure facts it cannot see — the gate verdict and section closure — never
# a fresh copy of its landing record.
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
     "item": {"id": "c-<timestamp>", "who": "reckon-build",
              "when": "<iso-now>",
              "body": "gate <gate-name> <verdict>; section <closure-state>"}}
  ],
  expected_version=state["version"]
)
```

That single state write carries the commit and gate measure alongside `impl`;
a moved percentage is never the only new information. Preserve the manifest's
exact measure and artifact paths rather than replacing them with “passed”. The
cumulative evidence record receives the worker's anchor with its merge; edit or
append to it in the same beat, never re-write what the worker authored.

**`impl`** = (count of completed executable nodes) / (count of total executable
nodes) over the whole selected plan (the orchestrator owns the denominator),
monotonic. Set it on EVERY node landing — the server does NOT compute it; you
MUST set it.

If the node also closes its section, include the driving-followup resolution
and the section's `section_declarations` reclassification to `done` in the same
`edit_plan` call, then collapse the section as §6b requires. If it does not
close the section, leave that followup open: a node landing advances the ledger
without pretending the larger section is finished.

**Same-plan follow-on work becomes a section, never a followup.** Before setting
a terminal status, add discovered work that belongs to this plan to the evergreen
as a concrete section, add its executable nodes to the DAG, and keep the plan
active. A followup is reserved for work owned by a different plan, whose own
lifecycle keeps it visible to `roadmap`.

### 7a-bis. Completed work is a RECORD, not a container — canonical rule

**A finished plan and a closed sprint are records of what happened. Neither is a
place to put new work.** This is the rule that governs where discovered work goes,
and it is authored here; `reckon-edit`, `reckon-sprint` and `reckon-create` refer
to it.

**Why it matters, mechanically.** A plan at `impl` 1.0 or status `shipped`/`done`
is excluded from `roadmap`'s `pending_work`, `ready_now`, `critical_path` and every
open path. So a followup appended to it is not *tracked* work — it is **hidden**
work. It renders on the plan page, it satisfies the continuation check, and it
appears nowhere a human or agent looks to decide what to do next. The same holds a
level up: an item added to a `done` sprint is invisible to the advancing horizon.
Squeezing work into a completed container is the most convincing way to lose it,
precisely because it leaves a paper trail that looks like tracking.

| Where the work was discovered | Where it goes |
|---|---|
| against a plan that is still `active`/`pending` | a **section** on that plan, with DAG nodes (§7) |
| against a plan that is **complete** | a **NEW plan**, linked to an advancing sprint |
| against a **closed** sprint | the **advancing** sprint — never back-filled |
| owned by a different live plan | a followup **pointer** on yours, naming that plan |

**The one legitimate followup on a completed plan is a pointer, never the work.**
Its body names the plan that carries the work and says nothing that would have to
be executed from where it sits. If you cannot name that owning plan, the followup
is hiding work and a plan is what you owe.

**Reopening a completed plan is not the escape hatch.** Flipping `shipped` back to
`active` to hang one more node on it is legitimate exactly once — when the plan's
own declared scope genuinely was not finished. Doing it repeatedly is the smell
that the work is a new subject wearing the old plan's name: each flip erases the
signal that the earlier scope closed, and readers can no longer tell which verdict
belongs to which question.

**The operational test, and it is falsifiable.** After writing the work down, call
`roadmap(project)` and look for it in `pending_work`. If it is not there, you did
not record work — you hid it. Fix it by creating the plan, not by rewording the
followup.

**Sprint horizon.** Keep the horizon advancing: when the active sprint's items are
all resolved, close it and open the next, rather than parking new work in a stale
`planned` sprint or a closed one. Move a plan between sprints only with its
prerequisites — moving a prerequisite behind its successor is a sprint-order
inversion and a failed rebalance.

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

There is a fifth disposition, and it is the only one that may end a session with
foldable work still on the queue:

- **`context-exhausted`** — the *orchestrator's own* budget cannot carry another
  fold. A legitimate, common handoff, but **reportable, not silent**: it must
  carry a figure — remaining context or token headroom, or the backend hold from
  §4c — and it applies to the SESSION, never to an item. Dressing a scope
  judgement in this label is the failure the figure exists to expose.

Do not invent a sixth category — not inconvenience, worker capacity, a
"different kind of work", or ordinary unfinished work.

After folding and executing the added nodes, re-read open followups before
testing for completion: landing that work may have generated more. Re-triage
after every landing beat and terminate only when a complete pass finds nothing
foldable. A fixed pass count, the end of the original DAG, or an empty
`follow_ons` field from one manifest is not the termination condition.

**The drain ledger — the check that makes this rule hold.** "A complete pass
found nothing foldable" is unfalsifiable unless the pass leaves an object behind,
so it writes one. Before the closing report, enumerate EVERY queue row and give
each a disposition from the closed set:

```text
DRAIN LEDGER — <plan|sprint> @ <iso-now>
  <id or one-line description>   folded → <node-id>
  <id or one-line description>   authority-required → <the authority needed>
  <id or one-line description>   dissent-reopen → <the locked decision>
  <id or one-line description>   foreign-owner → <owning plan or repo>
  <id or one-line description>   context-exhausted → <the figure>
  ---
  rows: N   foldable-remaining: 0   unreconciled-runs: 0
```

Write it into the landing beat's plan comment or the cumulative evidence record,
so it is committed rather than conversational. Three properties do the work:

- **A row with no disposition is an unfinished drain**, not a tidy stop. Continue.
- **`folded` requires a dispatched node id.** A row marked folded with no node is
  a stop with paperwork on it — the most convincing form of this failure, and the
  one a reader can now catch.
- **Both zeroes are the termination condition.** `foldable-remaining` is checked
  against the followup rows; `unreconciled-runs` comes from
  `crew(project, view="drain")`, never from memory. Record deliberate pointers
  first with `reckon crew drain --project <project> --leave
  <run-id>=handed-off|still-working`.

**Terminal status is gated on this drain.** Do not set `status` to `shipped` or
`done` while any foldable followup is open. Exempt rows may remain open only with
their recorded exemption; their presence is an explicit authority, dissent,
ownership, or context boundary rather than forgotten executable work.

### 7d. Fold depth is expected, and carries no stopping authority

**A fold chain routinely runs many layers deep, and depth feels like scope drift
from the inside.** Convergence and creep differ only in direction — test it:

- **Converging** — each layer's blocker is NARROWER than its parent's, the write
  scope is the same size or smaller, and the measure gets more exact. Continue.
- **Creeping** — each layer's blocker is WIDER, new subsystems keep entering the
  write scope, and the measure gets vaguer. That is a genuine scope change: stop,
  and raise it as its own plan rather than folding it.

**Do not count layers and do not budget them** — a ten-layer chain converging on a
one-line fix is healthy, and the deepest layers are usually the cheapest and
highest-leverage. The only quantity that governs stopping is the drain ledger's
`foldable-remaining`, and the only session-level exemption is `context-exhausted`
with its figure. Worked example: `references/sprint-orchestration.md` §10.

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
# crew(project, view="drain")["unreconciled_runs"] == 0
# version has incremented
```

For a sprint target, also verify every sprint item is done or explicitly
blocked, integrated worker commits are reachable from the primary branch, the
sprint summary links its plan/evidence outcomes, and no session worktree
remains. Re-run `roadmap(project, sprint=<id>)` at closure and require an empty
ready set and no error-level wiring findings before setting the sprint done.

For a graph target, re-run
`roadmap(project="graph:<handle>", view="raw")` at closure and verify its
`completion.shipped == completion.total`, its ready queue is empty, and its
decision blocker list remains empty. Record the final shipped-of-total,
critical path, average width, repositories reached, and schedule override
count in the endpoint plan's cumulative evidence. The graph owns no separate
lifecycle state; each member plan remains the authority for its own outcome.

Validate HTML integrity yourself — `uv run --project ~/Code/reckon reckon
audit-doc docs/plans/<slug>.html` must report no ERRORs before committing. This
checks the coordinator's own state write, which is why it is not a delegated
node; product tests are, and remain, worker nodes.

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
/reckon-build <slug> §<N>
/reckon-build <project>:<sprint-id>
```
````

**A "Next up" block naming work you had the authority, budget, and roster to do is
a defect, not a courtesy.** The block hands over only what genuinely cannot
continue here — an exempt row from the §7c ledger. A well-written handoff has
every surface feature of a delivery and is none of it. **Every entry must trace
to a ledger row disposed `authority-required`, `dissent-reopen`,
`foreign-owner`, or `context-exhausted`;** an entry tracing to a `folded` row,
or to no row, means the session stopped early and the block is the evidence.

Rules:
- One fenced prompt per advised follow-on; if several follow-ons are advised
  for one session, stack them in ONE fence in execution order.
- Every fenced line traces to an exempt ledger row. If the ledger says
  `foldable-remaining: 0` and no row is `context-exhausted`, there is nothing to
  hand over and the block is omitted entirely — that is the good outcome, not a
  missing section.
- The fenced line is exactly the slash invocation the next session needs. The
  plan owns all guidance, so never append a parenthetical brief or pasted wall.
- Mention the followup id at most once, in passing (e.g. "tracked as
  f-tps-03"), and always AFTER the plan/section name — the id is for plan
  audits, the name is for humans.

## §05 session handoff

> **Canonical §05 contract: `reckon-edit` SKILL.md.** Keep this copy in sync.

Every stored followup prompt and user-facing handoff is one line:

```
/reckon-build <slug> [§N]
```

The live plan owns all semantic guidance. Internal worker dispatches reference
that plan and add only the runtime safety fields required by
`references/sprint-orchestration.md`; they do not copy the plan into the
handoff.

## Delegation, runtime routing, integration, and cleanup

`references/worker-protocol.md` records everything true of a worker regardless of
backend — the eight-property contract, the four fences, the manifest shape, the
recovery ladder and the escape hatch. The engine injects that content; read the
reference only when hand-composing a delegation Reckon did not prepare.
(`references/worker-backends.md` is maintainer documentation of the translation
internals; an orchestrator never needs it.)

Every node is delegated on all targets. The skill carries the fixed session
contract; use `references/sprint-orchestration.md` only when hand-composing. It
expands on:

- prompt-owned runtime model, effort, and concurrency routing;
- skill and reasoning-effort selection;
- detached worktree creation and worker prompt rules;
- orchestrator-owned merge/conflict handling;
- research-before and evidence-after gates;
- reachability checks and mandatory worktree cleanup.

Use `scripts/worktree_fleet.py` for deterministic worktree creation, inspection,
and cleanup. `reckon crew gc` reports disposable crew workspaces and removes
only on request, so auditing what is reclaimable is free. Workers never mutate
shared Reckon state; the orchestrator records followups, evidence, plan
progress, sprint item outcomes, and sprint closure after integration.

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
