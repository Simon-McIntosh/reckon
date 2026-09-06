# Sprint orchestration reference

Read this file only when hand-composing a delegation Reckon did not prepare. The
engine-generated dispatch already carries the binding worker contract. Both
targets delegate every node, so everything here binds a single-plan coordinator
exactly as it binds a sprint one; only target resolution and sprint writeback are
sprint-specific. Where the text says "sprint coordinator", read "coordinator".

`worker-protocol.md` is its companion and is not repeated here: it owns the
eight-property task contract, the four fences, the manifest shape, the recovery
ladder and the escape hatch — everything true of a worker regardless of which
backend runs it. This file owns what the *orchestrator* does around that.

## Contents

1. Target resolution
2. Execution graph and knowledge inputs
3. Coordinator-only contract and context budget
4. Worker requirements and runtime routing
5. Worktree-first delegation
6. Worker dispatch contract
7. Orchestrator integration
8. Plan, evidence, and sprint writeback
9. Cleanup and recovery

## 1. Target resolution

Resolve the invocation before reading implementation files:

| Invocation | Meaning |
|---|---|
| `/reckon-ship S1` | Sprint `S1` in the current repository project |
| `/reckon-ship nova:S1` | Sprint `S1` in project `nova` |
| `/reckon-ship sprint:S1` | Explicit sprint in the current project |
| `/reckon-ship plan:solver-hardening` | Explicit plan |
| `/reckon-ship solver-hardening` | Plan slug |

Derive the current project from the repository and `docs-project` metadata.
Read `read_plan(project, view="summary")`; match sprint ids exactly. Then read
the selected sprint with
`read_plan(resource={project,type:"sprint",id:sprint_id}, view="raw")`.
If the summary reports `state.source_format="legacy-index"`, that raw sprint is
a compatibility projection: the orchestrator must version-pair sprint
writeback through `slug="index"` until the explicit distributed-state
migration activates named resource writes.
If an unqualified
identifier matches both a plan and a sprint, require the explicit prefix.

The orchestrator runs from the canonical primary checkout. It owns `index`,
plan followups, evidence links, merges, pushes, and sprint closure.

## 2. Execution graph and knowledge inputs

Before dispatch:

1. Call `roadmap(project, sprint=sprint_id)`. Treat its pending set,
   prerequisite-first paths, sprint sequence, and wiring findings as the
   canonical plan-level graph.
2. Stop graph construction for error-level wiring findings. Repair invalid,
   dangling, non-executable, inactive, contradictory, cyclic, sprint-order, or
   membership faults through the owning plan/sprint skill; then rescan.
3. Read the full sprint object and preserve item order as a priority hint after
   the dependency order returned by `roadmap`.
4. Read every member plan with `with_schema=True` and its complete HTML.
5. Expand only hard `depends_on` edges already validated by the analyzer.
   - Shipped/done prerequisites are evidence inputs.
   - Actionable same-project prerequisites become execution nodes.
   - Cross-project, missing, abandoned, or authority-expanding prerequisites
     are blockers unless already satisfied.
   - Research, specifications, and evidence are `informs` inputs, never
     executable prerequisites.
6. Build section-level nodes where plan prose or followups expose independent
   deliverables. Otherwise use one node per plan.
7. Add dependency edges from section sequencing, shared files,
   decisions, and explicit triggers.
8. Topologically sort the enriched section/file graph into a ready queue. A new
   cycle is a blocker; report the exact cycle instead of breaking it
   heuristically.

Read the knowledge envelope for every node:

- standalone research docs whose `plan-informs` includes the plan;
- research items embedded in the plan;
- URLs/files named in the plan's research section;
- prior landed records under the owning typed root, normally
  `docs/evidence/archive/` for execution outcomes;
- evidence artifacts linked from the plan or sprint;
- resolved followup outcomes and locked decisions;
- applicable user, repository, and target-path `AGENTS.md` plus triggered skills.

Research feeds implementation. Prior evidence establishes the baseline and
prevents duplicate or regressive work. If a material decision lacks research,
schedule a research-only node before implementation. After integration, author
or update evidence that links to the source plan and records commits, tests,
environment, artifacts, quantitative results, and negative findings.

## 3. Coordinator-only contract and context budget

A sprint target is strict coordination, not an implementation role.

The coordinator may only:

- resolve and read plan, sprint, research, evidence, repository, and worker
  state needed to scope or review the work;
- build, update, and checkpoint the execution DAG and exclusive file scopes;
- create worktrees and dispatch, wait for, or message workers;
- audit worker manifests, commits, scoped diffs, test evidence, and artifacts;
- integrate or merge verified commits, push the primary branch, and resolve
  mechanical merge conflicts that require no product-code invention;
- write Reckon plan, evidence, followup, and sprint state;
- clean worktrees and report outcomes or blockers.

The coordinator must not inspect implementation details beyond what scoping or
review requires, edit product/source/test files, run tests, run paid domain
pipelines, run any operational pipeline, or repair worker code. Represent
every implementation, investigation, test execution, operational pipeline run,
and corrective repair as a worker node. Delegate every ready node; there is
no slot pool — a busy roster is grounds to register another member, never to
hold ready work. Cross-cutting work changes the task requirements
and scope; it never makes the sprint coordinator the implementation owner.

On worker failure, add and dispatch a corrective node with the failed
manifest, scoped diff, and missing done-when evidence. Do not reconstruct the
implementation in coordinator context. If no capable worker backend exists,
prefer pausing the node and continuing independent ready work. Inline fallback
is allowed only after reporting all of the following before implementation:

- why no worker backend is capable (member scarcity never qualifies — members
  are registered on demand);
- why pausing would prevent useful progress;
- the exact node and write scope;
- the estimated context cost and the coordinator context remaining after it;
- the checkpoint from which a fresh coordinator can resume.

Maintain a compact coordinator checkpoint after DAG creation and after every
landing beat: node statuses and edges, scope ownership, worker/worktree ids, commit
SHAs, integration state, plan versions, and next ready nodes. Reserve
coordinator context for integration, state writeback, cleanup, and reporting;
when that reserve is threatened, delegate missing analysis or pause instead of
reading deeper implementation detail.

Workers return a compact manifest:

```text
node: <stable node id>
status: complete | blocked | failed
commits: <sha list>
changed_paths: <explicit list>
tests: <concise command/result summary>
test_logs: <paths on disk>
artifacts: <paths/urls plus headline metrics>
evidence_inputs: <facts needed for Reckon writeback>
blockers: <none or exact unmet condition>
```

Keep large logs and artifacts on disk. Read manifests, `git show --stat`, and
summary diffs first; request only missing evidence from the worker. Never pull
a full log or artifact into coordinator context when a bounded excerpt or
on-disk path is enough.

### Durable delivery — the manifest goes to a file, not only to a message

**A worker's return message is not a reliable channel.** A background worker can
finish its work and then end its turn without delivering a final report: the
runtime signals the agent idle, the orchestrator sees no manifest, and the node
looks failed when it is not. Re-asking often produces another bare idle signal,
and redispatching repeats work that already succeeded.

So do not depend on the message channel. **Every dispatched worker writes its
manifest to the absolute path in the dispatch prompt, then replies with that
path plus a short summary.** Normal `reckon crew dispatch` calls omit
`--manifest`: the engine assigns the durable default below and returns it in the
run contract:

```text
<config-home>/crew/runs/<run-id>/manifest.md
```

Use `--manifest <absolute-durable-path>` only for an intentional override. Do
not place it under `/run/user`, another tmpfs scratchpad, or the worker's own
worktree. The orchestrator reads the file; the reply is a convenience, not the delivery.
This costs the worker one write and removes the failure mode entirely.

Detect and recover in this order, and do not redispatch first:

1. **Worker signals idle with no manifest** — check whether the manifest file
   exists. It very often does.
2. **No manifest file** — check for the other on-disk evidence the prompt
   required (test logs, artifacts, benchmark output). Their presence proves the
   work ran and shows how far it got.
3. **Evidence exists but no report** — message the worker and tell it to write
   the deliverable to the named path and reply with the path only. This recovers
   reports the message channel will not carry, including long ones.
4. **No evidence at all after a bounded wait** — only now treat the node as
   failed and dispatch a corrective worker.

Never redispatch on an idle signal alone. Confirm from disk first: a duplicate
run of a node that already succeeded wastes a member and its budget, and for a node with
write scope it risks a conflicting second commit.

Prompt-side rules that make this work:

- Preserve the engine-returned manifest path explicitly in the prompt. For a
  read-only node, state that it is the **only** file the worker may write.
- Tell the worker its final message is the return value, and that a long
  deliverable belongs in the file with the reply reduced to the path.
- Require every long-running command to redirect to a named on-disk log, so
  step 2 always has something to find.

## 4. Worker requirements and runtime routing

Read the versioned capability request from plan/followup/sprint state as task
requirements only. Its neutral `class` and optional floors for reasoning,
context, tool autonomy, verification, and risk describe what the node needs;
they do not select a model.

Set an explicit concurrency target in the current prompt or coordinator
checkpoint before dispatch. Derive it from dependency independence, file-scope
conflicts, and operational limits; Reckon defines no fixed default and no slot
pool — the only binding rule is one live run per roster member, so meet the
target by registering members. Use the single advisory fleet-size table in
`../SKILL.md`; do not restate it in this reference.

Runtime routing is prompt-owned:

1. Honour any model, reasoning-effort, or concurrency choice in the current
   user prompt.
2. Otherwise, the coordinator inspects the advertised workers and chooses a
   concrete model and effort for each node from its requirements, ambiguity,
   coupling, risk, and verification needs.
3. State the concrete model and effort in every worker dispatch. Do not infer
   them from the coordinator model or a relative hierarchy.
4. Keep provider names and concrete model identifiers out of plans, schemas,
   skills, source code, and persisted capability requests.
5. Re-evaluate routing when a task's scope changes; do not reuse a stale model
   choice merely because an earlier node used it.

Legacy plan `tier` values map on read for compatibility and emit an audit
diagnostic. They provide no runtime routing authority. Never copy them into a
new dispatch record; persist neutral task requirements when the plan is next
edited.

Score every task on:

- ambiguity and missing decisions;
- domain/safety risk;
- coupling and shared-file pressure;
- blast radius and reversibility;
- quality of objective tests and done-when criteria.

Select the worker's skill from task semantics and `recommends_skill`. Require
the worker to read the skill plus all applicable target-path instructions
before editing. Include task-requirement rationale, the explicit runtime model
and effort, their prompt/coordinator source, and selected skills in the
execution manifest.

If no worker is advertised, pause the node and record
`no-advertised-worker`; use the coordinator inline exception only under the
reported, pre-budgeted protocol in section 3. If the explicitly selected
worker cannot satisfy the task requirements, pause and surface the routing
mismatch rather than silently weakening the contract. Elevated or critical
risk raises the task's verification requirements; it does not turn the sprint
coordinator into the implementation owner.

## 5. Worktree-first delegation

Create one detached worktree per delegated node:

```bash
python skills/reckon-ship/scripts/worktree_fleet.py create \
  --repo <repo-root> --session <opaque-session-id> \
  --worker <opaque-worker-id> --base <primary-branch>
```

Detached worktrees avoid shared working-tree contamination and do not require
workers to create or switch branches. Create every worktree from the same
verified primary-branch base. A dependent node starts only from a revision that
contains its verified, integrated predecessors. An independent refill may start
from the current verified primary HEAD without waiting for unrelated active work.

Workers:

- stay inside their assigned worktree;
- edit only exclusive paths;
- never alter shared plan/index HTML;
- never merge, rebase, stash, or push the primary branch;
- stage explicit paths and make one or more coherent local commits, each
  carrying a subject and a body (see contract rule 6);
- return final commit SHA, `git show --stat`, tests, artifacts, and evidence
  inputs.

The sprint coordinator does not execute a node inline merely because it is
single-item, cross-cutting, or has no immediately advertised worker. Follow
the pause-first, reported, context-budgeted exception in section 3.

### Where worktrees live, and why it is not a per-node decision

`worktree_fleet.py` places a session's worktrees beside the repository — under
`<repo-parent>/.reckon-worktrees/<repo>-<digest>/<session>` — so a checkout
inherits exactly the repository's own visibility. Override the location with
`RECKON_WORKTREE_ROOT`; the runtime temporary directory is still recognised as a
legacy root, so sessions created before this default stay inspectable and
removable.

**The default matters because a worktree is only useful where the work runs.** A
runtime temporary directory is usually node-local memory: a batch scheduler, a
remote executor, or a second machine mounts the repository's filesystem but not
that, so a node submitting a job cannot reach its own checkout and nothing it does
inside the worktree fixes it. Memory-backed checkouts also draw on the same pool as
the processes reading them, so a wide fleet competes with its own test runs.

Two things the placement does not solve, and which stay per-node decisions:

- **Caches and scratch belong on node-local storage**, not beside the repository.
  Shared filesystems are slow for small-file churn and some do not implement the
  atomic no-clobber rename that content-addressed caches rely on. Point `TMPDIR`
  and any artifact cache at local disk while the checkout stays shared.
- **A measurement that must name its tree needs a checkout frozen at one revision**
  for the whole run. Session worktrees are already detached at a fixed base; the
  hazard is a node measuring the *primary* checkout, which advances whenever the
  coordinator pushes. A long run against a moving checkout cannot attribute its
  result however cleanly it finishes.

### Infrastructure a worker cannot provide itself

**A worker blocked on the shape of its own workspace is a coordinator failure, not
a node failure.** Provisioning worktrees, checkouts and execution locations is
coordinator work (section 3), so when a node needs something the default worktree
does not supply, the worker asks and the coordinator provides it — the same turn,
not as a followup, and never as a reason to abandon the node.

Requests in this class: a checkout on storage a particular executor can reach, a
longer budget for work that genuinely cannot be split, a reservation, a dataset
staged where the job can read it. Grant them as a scope extension — the worker
still commits in its own detached worktree, and an execution location is not a
second write scope. Clean up anything provisioned with the session, on the same
reachability terms as any other worktree.

**The test is whether the obstacle is inside the node's control.** If it is not,
supply the resource; never ask the worker to shrink the work until it fits,
because a measurement resized to fit a budget measures the budget. A node that
quietly reduces its own grid, sample or cohort to finish inside a fence has
changed what it was asked to demonstrate, and its manifest will report success
against work nobody commissioned.

## 6. Worker dispatch contract

`reckon crew dispatch` composes this contract, the four fences and the escape
hatch into the worker's prompt and writes it beside the run record, so a
configured backend needs none of it typed out. Compose it by hand only for a
delegation reckon did not prepare; when both exist, they must say the same thing,
and the composed prompt is the copy to change.

**A hand-composed delegation is strictly weaker, and not merely more typing.**
It has no live pointer, so nothing classifies it: `crew(view="live")` and `crew
recover` cannot see it, it never enters a drain count, it produces no ledger
record and no line on any follower's stream, and its worker's final message is
the only account of it there is. Every safety property in this file that reads
from a pointer is absent. So prefer registering the repository wherever Reckon's
scaffolding is acceptable there — and note that registering *write authority* is
a `mounts.json` entry in the config home, which touches nothing in the target
repository at all; only the UI scaffolding `reckon sync` also copies does. When a
repository must stay untouched, that entry alone admits the dispatch, and the
docs directory it names need not exist.

Hand-compose only when even that is inappropriate, and then read the worker's
success claim with the scepticism it has earned: measured, one such worker
completed a node, could not write the contracted manifest, wrote it elsewhere
and reported success — and with no pointer, nothing contradicted it.

**A hand-composed sandbox must make three things writable, and two of them are
outside the worktree.** A detached worktree's git index is not in the worktree:
it lives at `<main-repo>/.git/worktrees/<node>/index`, so a tier that permits
only the workspace cannot create `index.lock`, and the worker dies at `git
commit` having already done the work. The manifest path is outside it too. Grant
all three explicitly:

1. the assigned worktree;
2. the **main repository root**, which is what covers the worktree's git
   directory;
3. the directory holding the manifest path.

**Grant those roots rather than escalating the tier.** A workspace-only tier is
sufficient *once the two outside roots are added* — that addition is exactly
what `reckon crew dispatch` does when it resolves a restricted sandbox, and it
is why dispatch commits fine at that tier. So "dispatch manages at this tier" is
true and, on its own, misleading: reproduce the added roots, not just the tier
name. Reaching instead for a full-access tier buys the commit by giving the
worker more privilege than dispatch would, which is a different decision and
should be a deliberate one. The concrete per-backend flag is
`worker-backends.md`'s subject, not this file's.

Measured: a hand-composed dispatch at a workspace-only tier completed a node,
self-verified it, then failed with `fatal: Unable to create '…/index.lock':
Read-only file system` and delivered no commit — and because the mandated
manifest path was unreachable for the same reason, the worker wrote a fallback
elsewhere and reported success, so the orchestrator saw a completed node whose
delivery was not where the contract named. Rule 8 above now makes that a
blocker, because a delivery contract that degrades quietly is worse than one
that refuses.

Embed this contract in every delegated prompt:

```text
WORKTREE AND PARALLEL-SAFETY RULES (binding):
1. Work only in the assigned detached worktree. Do not create, checkout, or
   switch branches.
2. Never use git stash, git rebase, git clean, git reset --hard, or destructive
   path restoration.
3. Stage only explicit assigned paths. Never use git add -A, git add .,
   wildcards, git commit -a, or git commit -am.
4. Do not edit Reckon plan/index state. Return outcome data to the orchestrator.
5. Do not touch concurrent workers' paths. Request scope changes.
6. Commit locally with a conventional subject AND a body stating what
   changed and why — a bodiless commit fails the orchestrator's audit. Do
   not merge or push the primary branch.
7. Return the compact manifest from section 3 with commit SHA, git show --stat,
   concise test results and on-disk log paths, artifacts, and evidence inputs.
8. WRITE that manifest to the MANIFEST PATH below, then reply with the path and
   a short summary. Your final message is the return value, but the file is the
   delivery: do not end your turn with the manifest only in a message. If the
   deliverable is long, it belongs in the file and the reply is just the path.
   Redirect every long-running command to a named on-disk log so progress is
   recoverable even if your report is not. If that exact path is not writable,
   STOP and report it as a blocker — never write the manifest somewhere else.
9. Do not add AI attribution or plan/sprint identifiers to commit messages.
10. Stop and report unexpected dirty files, missing authority, or unsafe scope.

MANIFEST PATH (write this file before finishing):
  <config-home>/crew/runs/<run-id>/manifest.md

ASSIGNED WORKTREE:
  <absolute path>

EXCLUSIVE WRITE SCOPE:
  <paths>

CONCURRENT WORKERS:
  <worker → paths>
```

Also include:

- the live plan and section to read as semantic authority;
- selected skill(s);
- explicit runtime model/effort routing;
- only operational constraints that cannot live in the plan.

Do not restate plan context, decisions, research, or done-when criteria in the
worker prompt. Keeping those in the live plan prevents copied guidance from
drifting between workers and sessions.

## 7. Orchestrator integration

Integrate each verified completion as it becomes available:

1. Verify the worker worktree is clean.
2. Inspect the commit and exact changed paths.
3. Dispatch a verification worker to run targeted tests in the worktree, then
   audit its compact manifest and on-disk logs.
4. Commit any orchestrator-owned plan state before starting merges.
5. Merge worker commits sequentially into the primary branch with normal merge
   commits when they do not fast-forward.
6. Resolve only mechanical merge conflicts in the primary checkout. If
   resolution requires product/source/test edits or implementation judgment,
   dispatch a corrective integration worker. Preserve both independent intents;
   never discard one worker wholesale to make a conflict disappear.
7. Dispatch test workers for integration tests after each merge and broader
   tests at their dependency gates; audit their manifests.
8. Push the primary branch after each coherent integration.

If a worker edited out-of-scope paths, do not merge it blindly. Ask the worker
to split/rework the commit or dispatch a corrective worker in a new worktree.
If conflict resolution requires a new material decision, pause that dependency
branch and continue only independent ready nodes.

## 8. Plan, evidence, and sprint writeback

Each verified node has one landing beat: the orchestrator runs `reckon crew
complete`, then immediately writes that node to the plan. Do not promote another
run or merge another commit between those operations. The plan write is
mandatory, but dispatching an unrelated ready node is outside this freeze and
may refill a free slot. Workers return outcome data in their manifests and never
write shared plan or index state.

Immediately after each `reckon crew complete`, the orchestrator:

1. Updates the plan's cumulative evidence record at
   `docs/evidence/archive/<slug>-landed.html` and links the landed section to a
   stable anchor. Do not create per-node or per-section fragments; split a new
   evidence resource only for a materially independent artifact that stands on
   its own. Append the node's commit, gate verdict and quantitative measure,
   tests, artifacts, and negative findings to that anchor.
2. Calls `edit_plan` once with the advanced node-based `impl`, the commit added
   to `commits`, artifacts added to `artifacts`, and a section comment carrying
   the same commit, gate verdict, quantitative measure, and artifact paths. The
   number and its evidence travel in one version-safe state write.
3. Resolves the driving followup only when the node closes its section. A
   partially landed section remains open even though its plan ledger advanced.
4. Collapses a section in the evergreen only when its final node has landed.
5. Adds every manifest `follow_ons` entry to the same triage queue as the
   plan's open followups. Work owned by the plan in hand becomes a new evergreen
   section and executable DAG node, never a followup; only work owned by another
   plan may remain a followup that identifies that owning plan. Do not set a
   terminal status while a same-plan section remains open.
6. Re-reads the sprint resource and verifies its composed item status reflects
   the plan writeback. Item lifecycle status and implementation fraction are
   derived from plan HTML and must never be persisted in the sprint.
7. Appends a cross-plan followup when needed, or records `done — no followup`.

### Followup drain at every landing beat

The coordinator re-triages open followups after every landing beat, together
with the landed manifest's `follow_ons`. Manifest `follow_ons` enter the
same triage loop as open plan followups. Folding into the current orchestration
is the default; an entry may stay open only under one of these exemptions:

- **`authority-required`** — spend, an outward-facing effect, or an irreversible
  action needs authority the coordinator does not hold;
- **`dissent-reopen`** — the entry asks to reopen a locked decision and remains
  in the dissent flow;
- **`foreign-owner`** — another plan or repository owns the work and its
  lifecycle must surface it.

Every exempt open followup records which exemption it claims and the concrete
authority, decision, plan, or repository behind that claim. Capacity,
inconvenience, and ordinary unfinished work are not exemptions.

Session closure also drains current run pointers. Read
`crew(project, view="drain")`; its `unreconciled_runs` count is derived from the
live pointer directory, not reconstructed from dispatch memory. A pointer may
remain only after `reckon crew drain --project <project> --leave
<run-id>=<disposition>` records one of the closed-set dispositions:

- `handed-off` — another session owns reconciliation;
- `still-working` — the current live classification is `running`. This
  disposition stops excusing the pointer as soon as the run turns terminal.

Every other pointer counts. Promotion removes a run from the drain by deleting
its pointer. The committed closure ledger records both termination figures on
one line:

```text
  rows: N   foldable-remaining: 0   unreconciled-runs: 0
```

Fold eligible entries into sections and DAG nodes, execute the newly ready
nodes, then re-read the plan because their landings may have created more
followups. The loop runs after every landing beat and terminates only when a complete
pass finds nothing foldable; never use a fixed pass count or the original DAG's
end as the stopping condition. Do not set a plan to `shipped` or `done` while a
foldable followup is open. An exempt followup may remain open only when its
recorded claim satisfies one of the three categories above.

After all executable nodes:

- re-run `roadmap(project, sprint=sprint_id)` and record lifecycle plus stored
  implementation completion;
- re-read every member plan and the composed index view;
- ensure no ready or in-progress node is omitted;
- require no error-level wiring findings;
- record blocked/deferred nodes explicitly;
- write a sprint summary with plan and evidence links by editing the named
  sprint with its current resource version;
- **report the sprints this one feeds**, taken from the closing sprint's
  `feeds_sprints` and `unblocks` in the `roadmap` response. Both are derived from
  the dependency graph, so they cannot go stale the way a written list would, and
  a sprint that feeds nothing says so rather than staying silent. This is the
  sprint end of the three-altitude continuation rule; the worker and plan ends are
  the manifest's `follow_ons` and the refused-without-continuation writeback;
- set that sprint resource to done only when all non-deferred nodes are complete.

## 9. Cleanup and recovery

After each worker commit is integrated, and always before ending the session:

```bash
python skills/reckon-ship/scripts/worktree_fleet.py cleanup-session \
  --repo <repo-root> --session <opaque-session-id> \
  --integrated-into <primary-branch>
```

Cleanup preflights every session worktree. It refuses to remove a worktree when:

- tracked or untracked changes remain;
- its HEAD is not reachable from the integrated primary ref;
- the path is not registered to the repository.

Never force cleanup. A refused cleanup is a visible blocker with a recoverable
path and commit. After successful cleanup, run `git worktree list` and confirm
no session path remains.

If the orchestrator is interrupted, a new session can inspect the execution
checkpoint, `git worktree list`, worker HEADs, plan versions, and sprint state;
integrate or recover each worktree before resuming the DAG.

## 10. Wave reports and fold depth — worked examples

Read this only when hand-composing reports or judging a deep fold chain; the
binding rules live in the skill (§4e summary reflex, §7d fold depth).

### Wave reports on the four axes

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

### Fold depth — a real converging chain

Attach refuses on lifecycle → review the targets → review refuses on admission →
adjudicate the defects → the defects are validator bugs → fix the validators →
re-stage → persist. Eight layers, every one legitimate, every one narrowing the
blocker; measured on a real sprint it went ten. Each refusal hands over a
smaller and better-specified problem, which is why depth feels like scope drift
while actually converging. The corollary: the deepest layers are usually the
cheapest and highest-leverage — by layer six the problem has been narrowed by
five successive refusals, so the fix is often small and general (a
false-positive validator, a wrong call signature, a too-narrow guard) and
unblocks everything stacked above it. Abandoning a chain near its bottom
discards the most specific diagnosis anyone has ever had of that problem.

## 11. Prior-art reconnaissance

Plans go stale, and agents are quick to rebuild what already exists. Between
reading the plan and authoring any implementation node: audit plan currency
### 1b. Currency audit and prior-art reconnaissance — MANDATORY

Plans go stale, and agents are quick to rebuild what already exists. Between
reading the plan and authoring any implementation node:

**Audit plan currency against the code.** Check every asserted API, file,
limitation, and technical property against the named tree before cutting nodes.
A node authored from stale text executes its defects faithfully.

**Dispatch a prior-art scout in the background.** One read-only
investigate-role node, dispatched through `reckon crew dispatch` exactly like
every other node — never a harness-native background agent (that bypasses the
run ledger, manifest contract, and calibration) and never inline; register a
fresh roster member if every existing one is busy. Launch it at pre-flight so
it runs while the coordinator finishes reading state. Its single deliverable is
a REUSE MAP: the modules, symbols, tests and data already in reach that solve
the problem in whole or part, each with a one-line fitness verdict. It searches
capability-shaped, not filename-shaped ("2-D interpolation", not "does
fsa_kernel.py exist"), across this repository, every repository named by the
plan's external `depends_on` / `blocks` refs, and every repository this repo's
AGENTS.md declares as coupled — coupling runs both ways.

**Nodes cite the reuse map.** Each implementation node names the machinery it
extends or consumes. New machinery requires a fitness verdict explaining why
each named reuse candidate fails; otherwise it creates a competing authority.

## 12. Stop discipline — these are not valid reasons to stop

### These are not valid reasons to stop

Continue through ordinary complexity, validation, and recoverable integration:

| Rationalization | Reality |
|---|---|
| "This is risky or touches core code" | Route to a capable worker; test and validate integration. |
| "Better to confirm the approach" | The plan and locked decisions are the approval. |
| "This is long or needs full validation" | Continue; validation is a worker node. |
| "I'll offer options" | Follow the locked choice unless a genuinely new decision surfaced. |
| "A worker stopped at its fence" | Answer `NEEDS-HELP:` and resume that session, or reshape and redispatch the node. |
| "Infrastructure timed out" | Verify outage versus latency, then repoint with the corrected runtime brief. |
| "Two attempts failed" | Reshape or split the node; repeated worker failure is not plan abandonment. |
| "This next step is a *different kind* of work" | Not an exemption. §7c's list is closed; a new category is the stop wearing a costume. |
| "Each fold surfaced another fold" | That is convergence, not creep. See §7d — fold depth carries no stopping authority. |
| "The handoff is written, the next link is named" | A named next link you could dispatch is a defect, not a deliverable. See §9. |

Plans do not override global safety or expand user authority. A locked decision
settles implementation choices only inside the already-authorised scope.

**Hiccup or blocker — the one test.** If existing authority can move the work
forward by changing runtime, member, scope, node shape, advice, or experiment,
repoint and continue. Stop only when progress needs new authority: an external
prerequisite, user-owned decision, excess spend, or consent for an outward effect.

## 13. Continuity — who receives the next piece of work

A node's worker holds context no fresh worker can rebuild cheaply. Route by what
the next piece of work *is*, not by whichever worker is convenient:

| Next work | Goes to | How |
|---|---|---|
| A `NEEDS-HELP:` brief from a CLI-launched live run | that same run's session | `reckon crew resume --run <id> --advice "…"` |
| A `NEEDS-HELP:` brief from an in-harness run | that attached harness task/session | answer it through the host harness; CLI resume cannot launch it |
| A followup on work that just landed — review comment, gate evidence, a fix within the node's own scope | the **same worker**, via its roster member's long-lived session | `reckon crew dispatch … --member <id>` |
| New scope, a different file set, or significant rework | a **fresh dispatch**, its own worktree and node | `reckon crew dispatch …` with a new node id |

**This works only if the original dispatch named `--member`** — the session
survives in the member's committed `crew.json` entry after the live pointer
goes. **So dispatch every node as a roster member by default** (`reckon crew
member list` / `member add`; a member without a session captures its first
run's). Scope decides continuity: work inside the landed node's paths returns
to its member; wider scope is a new node recording `--scope-changed`. **A
member is a serial worker** — dispatch refuses one with a non-terminal
in-flight run, so independent concurrent work uses distinct members.

**Do NOT stop at routine checkpoints.** Keep going and update state as work
lands. Valid early stops are:
- A prerequisite plan is unshipped (hard stop — see §Prerequisite blocking)
- A NEW decision surfaced that is not already locked in the plan, is material to the work, and cannot be deduced from the plan/code/sensible defaults (an already-locked decision is NOT a reason to stop — honour it and proceed). When stopping for one, present it — and any other open decision the lead could settle in the same breath — as a structured multiple-choice question with a marked recommendation and per-option reasoning, per `reckon-edit` SKILL.md (*Interactive walkthrough*). Never hand back a bare list of open decision keys.
- The next section's scope would require writing files outside your allocated write scope
- Applicable safety policy or user authority requires confirmation
- A worker commit cannot be integrated safely without overwriting unrelated work

## 14. Target resolution mechanics

### 0. Resolve plan, sprint, or graph

1. Derive the current project from the repository/mount context.
2. Treat `graph:<handle>` as a graph target. Call
   `roadmap(project="graph:<handle>", view="raw")`; never reproduce its
   traversal or persist its returned `members`. Refuse an error response or any
   returned `decision_blockers`. Read every returned member plan, report the
   `schedule_override.deferred` count and member refs, then use the coordinator
   workflow over the returned ready queue across its mounted repositories.
3. Otherwise call `roadmap(project)` and match the argument against exact sprint ids.
4. Treat an exact sprint match, `sprint:<id>`, or `<project>:<id>` as
   a sprint target, and `plan:<slug>` as an explicitly selected plan target.
5. For every remaining bare token matching
   `[A-Za-z0-9][A-Za-z0-9._-]*`, try the canonical
   `roadmap(project="graph:<token>", view="raw")` read. A unique live claim makes
   it a graph target. A response that the handle names no live plan means the
   token remains a single-plan target; duplicate claims and every other graph
   error are refusals, never plan fallbacks. A token outside the handle grammar
   is also a plan target; authoring it as `plan-graph-handle` is refused with
   the grammar stated.
6. If a sprint target, resolve the sprint and use the same coordinator workflow
   over its complete graph. Do not continue with the plan-only preflight below.
