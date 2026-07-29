# Sprint orchestration reference

Read this file completely for sprint execution or whenever `reckon-ship`
delegates plan work.

## Contents

1. Target resolution
2. Execution graph and knowledge inputs
3. Coordinator-only contract and context budget
4. Worker capability and skill routing
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

1. Read the full sprint object and preserve item order as a priority hint.
2. Read every member plan with `with_schema=True` and its complete HTML.
3. Expand each plan's `depends_on` recursively.
   - Shipped/done prerequisites are evidence inputs.
   - Actionable same-project prerequisites become execution nodes.
   - Cross-project, missing, abandoned, or authority-expanding prerequisites
     are blockers unless already satisfied.
4. Build section-level nodes where plan prose or followups expose independent
   deliverables. Otherwise use one node per plan.
5. Add dependency edges from plan metadata, section sequencing, shared files,
   decisions, and explicit triggers.
6. Topologically sort into ready waves. A cycle is a blocker; report the exact
   cycle instead of breaking it heuristically.

Read the knowledge envelope for every node:

- standalone research docs whose `plan-informs` includes the plan;
- research items embedded in the plan;
- URLs/files named in the plan's research section;
- prior landed records under `docs/archive/`;
- evidence artifacts linked from the plan or sprint;
- resolved followup outcomes and locked decisions;
- applicable user, repository, and target-path `AGENTS.md` plus triggered skills.

Research feeds implementation. Prior evidence establishes the baseline and
prevents duplicate or regressive work. If a material decision lacks research,
schedule a research-only node before implementation. After integration, author
or update evidence that links to the source plan and records commits, tests,
environment, artifacts, quantitative results, and negative findings.

## 3. Coordinator-only contract and context budget

Full sprint mode is strict coordination, not an implementation role.

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
and corrective repair as a worker node. Delegate even one ready node whenever
a worker slot is available. Cross-cutting work changes the worker capability
and scope; it never makes the sprint coordinator the implementation owner.

On worker failure, add and dispatch a corrective node with the failed
manifest, scoped diff, and missing done-when evidence. Do not reconstruct the
implementation in coordinator context. If no capable worker or slot exists,
prefer pausing the node and continuing independent ready work. Inline fallback
is allowed only after reporting all of the following before implementation:

- why no worker capability or slot exists;
- why pausing would prevent useful progress;
- the exact node and write scope;
- the estimated context cost and the coordinator context remaining after it;
- the checkpoint from which a fresh coordinator can resume.

Maintain a compact coordinator checkpoint after DAG creation and after every
wave: node statuses and edges, scope ownership, worker/worktree ids, commit
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

## 4. Worker capability and skill routing

Use the versioned capability request from plan/followup/sprint state and resolve
it with `reckon.capability.match_worker`. The persisted object has a neutral
`class` plus optional hard floors for reasoning, context, tool autonomy,
verification, and risk. Concrete worker identity and cost stay runtime-only.

Use a model-family-neutral **one-below** policy:

1. Inspect the runtime's advertised worker models/capabilities. Do not encode
   provider or model names in the skill, plan, or dispatch logic.
2. Identify the orchestrator's capability position within its current model
   family.
3. Default each implementation worker to the immediately lower capable
   general-purpose model in the same family.
4. If no lower model is available, inherit the orchestrator model and reduce
   reasoning effort by one supported level.
5. Keep or escalate the worker to orchestrator-level capability for high
   ambiguity, solver/physics correctness, coupled multi-file refactors,
   security/safety, irreversible migrations, conflict resolution, and
   synthesis across workers.
6. Downshift further only for bounded mechanical edits, inventory reads, or
   research extraction with objective verification.
7. Never cross model families unless the user requests it or the runtime offers
   no suitable same-family worker.

Legacy plan `tier` values map deterministically on read and emit an audit
diagnostic. Never copy them into a new dispatch record; persist the mapped
capability request when the plan is next edited.

Score every task on:

- ambiguity and missing decisions;
- domain/safety risk;
- coupling and shared-file pressure;
- blast radius and reversibility;
- quality of objective tests and done-when criteria.

Select the worker's skill from task semantics and `recommends_skill`. Require
the worker to read the skill plus all applicable target-path instructions
before editing. Include chosen capability rationale and selected skills in the
execution manifest.

If no worker is advertised, pause the node and record
`no-advertised-worker`; use the coordinator inline exception only under the
reported, pre-budgeted protocol in section 3. If none satisfies every hard
floor, selecting the strongest advertised worker is only a diagnostic
fallback: `escalation_required` remains true, so the dispatcher must not
silently weaken the task contract. Elevated or critical risk raises the worker
floor to orchestrator-class capability with strict verification; it does not
turn the sprint coordinator into the implementation owner.

## 5. Worktree-first delegation

Create one detached worktree per delegated node:

```bash
python skills/reckon-ship/scripts/worktree_fleet.py create \
  --repo <repo-root> --session <opaque-session-id> \
  --worker <opaque-worker-id> --base <primary-branch>
```

Detached worktrees avoid shared working-tree contamination and do not require
workers to create or switch branches. Create every worktree from the same
verified primary-branch base unless a dependency wave has already integrated;
later waves start from the newly integrated primary HEAD.

Workers:

- stay inside their assigned worktree;
- edit only exclusive paths;
- never alter shared plan/index HTML;
- never merge, rebase, stash, or push the primary branch;
- stage explicit paths and make one or more coherent local commits;
- return final commit SHA, `git show --stat`, tests, artifacts, and evidence
  inputs.

The sprint coordinator does not execute a node inline merely because it is
single-item, cross-cutting, or has no immediately advertised worker. Follow
the pause-first, reported, context-budgeted exception in section 3.

## 6. Worker dispatch contract

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
6. Commit locally; do not merge or push the primary branch.
7. Return the compact manifest from section 3 with commit SHA, git show --stat,
   concise test results and on-disk log paths, artifacts, and evidence inputs.
8. Do not add AI attribution or plan/sprint identifiers to commit messages.
9. Stop and report unexpected dirty files, missing authority, or unsafe scope.

ASSIGNED WORKTREE:
  <absolute path>

EXCLUSIVE WRITE SCOPE:
  <paths>

CONCURRENT WORKERS:
  <worker → paths>
```

Also include:

- plan and section;
- research/evidence inputs already read;
- locked and open decisions;
- selected skill(s);
- capability/effort rationale;
- measurable done-when criteria.

## 7. Orchestrator integration

Integrate one completed wave at a time:

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
7. Dispatch test workers for integration tests after each merge and the
   broader gate after the wave; audit their manifests.
8. Push the primary branch after each coherent integrated wave.

If a worker edited out-of-scope paths, do not merge it blindly. Ask the worker
to split/rework the commit or dispatch a corrective worker in a new worktree.
If conflict resolution requires a new material decision, pause that dependency
branch and continue only independent ready nodes.

## 8. Plan, evidence, and sprint writeback

After each integrated node, the orchestrator:

1. Writes the stage evidence/landed record and links it to its source plan.
2. Resolves the driving followup with commit, tests, and quantitative outcome.
3. Advances plan implementation fraction monotonically.
4. Collapses fully landed sections in the evergreen plan.
5. Re-reads the sprint resource and verifies its composed item status reflects
   the plan writeback. Item lifecycle status and implementation fraction are
   derived from plan HTML and must never be persisted in the sprint.
6. Appends the next followup, or records `done — no followup`.

After all executable nodes:

- re-read every member plan and the composed index view;
- ensure no ready or in-progress node is omitted;
- record blocked/deferred nodes explicitly;
- write a sprint summary with plan and evidence links by editing the named
  sprint with its current resource version;
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
