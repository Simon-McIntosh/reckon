# Worker protocol

Read this reference only when hand-composing a delegation Reckon did not prepare;
the generated dispatch already carries this binding protocol.

Everything true of a worker regardless of which backend runs it: what makes a
node dispatchable, the four fences a dispatch carries, the manifest it returns,
and how it asks for help instead of thrashing.

One home for these is what stops them drifting apart between backends. Nothing
here is harness-specific — launch mechanics and session handling live in
`worker-backends.md`, and neither file repeats the other.

## 1. The task-definition contract

**A worker that thrashes is almost always executing a malformed task.** So
well-formedness is checkable *before* dispatch, which leaves the escape hatch
handling only the genuine residual. Run this checklist over every node; a node
that fails is reshaped or split, never dispatched in the hope that the worker
will work it out.

A node is dispatchable only when all eight hold:

| Property | Test |
|---|---|
| Single goal | The goal states one deliverable. If it joins two with "and", it is two nodes. |
| Fully specified | Every input is already in the live plan or in the node's fences. Nothing requires the worker to infer intent. |
| Demonstrable | Done-when is a *measure* that emits evidence — a named test, a recorded command output, a numeric result against a stated bound. A subjective adjective ("clean", "robust", "better") fails this test. |
| Closed | No decision from outside the node is needed. A required-but-unlocked decision means a decision node precedes it. |
| Scoped | Exclusive write paths are enumerated and no concurrent node shares a path. This checks exclusivity, not sufficiency: it does not prove the named paths can carry the goal. |
| Bounded | The work fits the resolved time budget. If it cannot, split it — a budget is not a target to overrun. |
| Independently verifiable | The orchestrator can audit completion from the manifest, `git show --stat` and the gate evidence, without reading the implementation. |
| Spec level | The node declares one of `exact`, `guided` or `open`; a run without one cannot enter a calibration slice, so an undeclared node is refused, and a level outside that set is refused at dispatch too. |

`reckon crew dispatch` enforces the same eight and refuses a node that fails,
naming every failing property in one pass so the node can be reshaped in one
edit. `--dry-run` runs the identical resolution and validation without creating
a worktree or a process. Its payload reports the resolved write paths explicitly,
so a coordinator can inspect the actual exclusivity fence before sending a whole
wave. The coordinator still owns the sufficiency review: dry-run cannot infer
that the named paths contain every implementation surface the goal will need.

Two properties are worth stating plainly because they are the ones a hurried
dispatch skips.

**A subjective done-when is not a measure.** "Make the dispatch path robust"
cannot be audited, cannot be gated and cannot be reported quantitatively at
completion. Name what would be observed instead.

**A relative manifest path is invisible to the orchestrator.** It resolves
against the worker's own worktree, so the orchestrator looks somewhere the file
will never be, reads a delivered node as silent, and redispatches work that
already succeeded. Manifest paths are absolute. `reckon crew dispatch` supplies
the durable default under `<config-home>/crew/runs/<run-id>/manifest.md`; an
explicit `--manifest` is only an absolute durable override.

## 2. The four fences

Each dispatch carries four fences and nothing else. The live plan remains the
semantic authority; copying plan prose into a prompt creates a second source of
truth that drifts between workers and between sessions.

| Fence | Content |
|---|---|
| Scope | exclusive write paths; no two concurrent workers share a file |
| Time | an explicit budget; exceeding it means stop and report, never push on |
| Evidence | the gate's measure *is* the done-when, stated quantitatively |
| Delivery | a named manifest path on disk; long output goes in the file and the reply is the path |

## 3. Delivery is a fence, not a convention

**A background worker can finish its work and end its turn without delivering a
report.** The runtime then signals the agent idle, the orchestrator sees no
manifest, and the node looks failed when it is not. Re-asking often produces
another bare idle signal, and redispatching repeats work that already succeeded —
which for a node holding write scope risks a conflicting second commit.

Requiring the manifest on disk removes the failure mode for the cost of one
write. The file is the delivery; the reply is a convenience.

Recover in this order, and do not redispatch first:

0. **Zero-length stream** — the run never started. `observe` reports `orphaned`,
   which is indistinguishable from a mid-work crash until you read `stderr.log`
   in the run directory; a launch that failed writes its reason there and
   nowhere else. A session-lock collision from reusing a busy roster member is
   the common cause. Redispatch is correct here, and only here, as a first move.
1. **Idle with no report** — check whether the manifest file exists. It very
   often does. `reckon crew observe --run <id>` reports this as
   `manifest_present`.
2. **No manifest** — check the other on-disk evidence the prompt required (test
   logs, artifacts, benchmark output). Their presence proves the work ran and
   shows how far it got.
3. **Evidence but no report** — tell the worker to write the deliverable to the
   named path and reply with the path only. This recovers reports the message
   channel will not carry, including long ones.
4. **Nothing at all after a bounded wait** — only now treat the node as failed
   and dispatch a corrective worker.

A run whose process has exited with no terminal event in its log is a
recoverable orphan rather than a finished run; `observe` reports it as
`orphaned` rather than guessing either way.

## 4. The manifest

```text
node: <stable node id>
status: waiting | complete | blocked | failed
commits: <sha list>
changed_paths: <explicit list>
tests: <concise command/result summary>
test_logs: <paths on disk>
baseline_suite: <inline JSON observation when the project suite is armed, otherwise none>
after_suite: <inline JSON observation when the project suite is armed, otherwise none>
artifacts: <paths/urls plus headline metrics>
evidence_inputs: <facts needed for reckon writeback>
follow_ons: <work discovered but fenced out of scope, or none>
blockers: <none or exact unmet condition>
```

`tests` remains the concise human summary. When flight configuration declares
`gates.suite_command`, `baseline_suite` and `after_suite` are both required and
each is one inline JSON object with these fields:

```text
{"revision":"<sha>","command":"<exact command>","exit_status":<integer>,"log_path":"<path or empty>","log_digest":"<digest or empty>","completed":<true|false>,"failure_count":<integer>,"failure_ids":["<test id>"]}
```

At least one of `log_path` or `log_digest` is required. `completed=false`
means the suite result is absent — collection failure, cancellation, skipping,
or another unfinished measurement — and must never be interpreted as an empty
failure set. `revision` binds each observation to the tree it measured;
`failure_count` must equal the number of `failure_ids`.

`follow_ons` is the worker end of the continuation chain. Work a worker
discovered but was fenced out of otherwise has nowhere to go but prose, where it
is lost. The orchestrator either folds each candidate into the current wave or
writes it as a plan followup; `crew.followup_ops_from_manifest` builds the append
ops so the invocation line stays canonical.

### The declared external wait

**When your work submits a job it does not control — a batch job, a queued build,
anything whose completion it would otherwise sit and check — write the wait into
the manifest and exit, rather than polling the job from inside the turn.** The
run then reads as waiting rather than as working or stalled, and `resume-ready`
wakes it the moment the probe reports a terminal value. A declared wait is quiet
without being dead; a poll loop is neither.

`status: waiting` plus four fields make the declaration:

| Field | What it carries |
|---|---|
| `wait_condition` | one line stating what is being waited on — the submitted job and what must happen for the wait to end |
| `wait_probe` | the shell-free argument vector that answers the condition, run in your worktree |
| `wait_terminal` | the probe's output values that mean the wait is over |
| `resume_brief` | what the resumed self does next, in the same voice as the rest of the manifest |

`wait_probe` runs without a shell, and its last non-empty output line is matched,
case-insensitively, against `wait_terminal` — with `exit:<code>` standing in when
the probe prints nothing. A state never listed there leaves the run waiting.
`wait_started_at` dates the wait and is optional: the manifest's own modification
time is the fallback, so write the manifest once and leave it alone. A resume
needs the worktree, so the fields must be enough for a fresh eye there to finish
the node.

**Why polling is the wrong default.** Every check of a running job is a full
model round-trip, and on this fleet each round-trip re-sends the whole context —
measured at roughly 180,000 billed tokens per command on a node that submitted
no job, and roughly 280,000 on one that did, on a metered lane where better than
98 percent of the input was cached. The run that waited just over ninety minutes
billed 46 million of its 51 million input tokens in the turn that did the
waiting, because that turn *was* the poll loop. A declared wait charges a few
tokens once, for the manifest, and nothing for the hours after. Those figures
are a dated illustration of the mechanism, not a standing rate: the cost scales
with context size and model.

**The boundary.** A short bounded wait inside a turn — a counter that bumps a few
times and resolves — is fine and simpler. This is for waits long enough that
polling them costs more than a resume.

Keep large logs and artifacts on disk. The orchestrator reads the manifest,
`git show --stat` and bounded excerpts — never a whole log pulled into its
context.

## 5. Worktree and parallel-safety rules

Binding on every worker, and embedded verbatim in every dispatch prompt:

1. Work only in the assigned detached worktree. Do not create, checkout or
   switch branches.
2. Never use `git stash`, `rebase`, `clean`, `reset --hard`, or destructive path
   restoration.
3. Stage only explicit assigned paths. Never `git add -A`, `git add .`,
   wildcards, `git commit -a`, or `git commit -am`.
4. Do not edit reckon plan or index state. Return outcome data to the
   orchestrator.
5. Do not touch a concurrent worker's paths. Request a scope change instead.
6. Commit locally with a conventional subject **and** a body stating what
   changed and why. Do not merge or push the primary branch.
7. No AI attribution, and no plan, sprint or ticket identifiers in commit
   messages, symbol names, filenames or comments.
8. Write the manifest to the named path before finishing, then reply with the
   path and a short summary.
9. Stop and report unexpected dirty files, missing authority, or unsafe scope.

Workers default to full access bounded by their detached worktree. A filesystem
sandbox is inherited by child processes and breaks test runners, builds and
anything spawning subprocesses — so the worktree is the blast-radius boundary
instead. For dialects with a filesystem sandbox, the reviewed read-only tier
runs from the delivery directory under workspace-write: the manifest and other
delivery files remain writable while the repository at the assigned worktree
path remains read-only. This guarantees that the sandbox never blocks the
manifest itself. A node that must execute builds or test runs still belongs in
worktree-full, because caches outside the delivery directory remain read-only.

## 6. The live run record

Dispatch writes a JSON pointer under the crew home the moment a worktree
exists, before the worker has produced anything — this is what `reckon crew
observe`/`resume` read, and it is a different document from the manifest in
§4: the manifest is the worker's own report of what it did, the run record is
the orchestrator's account of where and how it is running. Recovery in §3
reads this record first, before assuming a silent worker has failed.

| Field | What it identifies |
|---|---|
| `run_id` | The stable id for this dispatch; keys the pointer file itself and the run's on-disk directory. |
| `worktree` | The detached worktree path the worker is confined to — the blast-radius boundary named in §5. |
| `log_path` | The machine-readable event stream (`stream.jsonl`) a backend writes as it runs; `observe` derives `phase` and `budget` from this file, not from asking the worker. |
| `manifest_path` | The absolute path the worker must write its manifest to — copied from the node's own fence (§1, §2) so the pointer and the dispatch agree on where delivery lands. |
| `phase` | `starting` until the first event arrives, then whatever the backend's stream reports; a stream that stops without a terminal event reads as still working rather than failed, because only the process table can tell the difference. |
| `session_id` | The backend's resumable session identifier, captured once known; this is what makes `reckon crew resume --run <run-id> --advice ...` (§7) continue the *same* session rather than starting a fresh one that has to rebuild context from nothing. |
| `budget` | The backend's reported usage/headroom, or an explicit unknown-reason placeholder when a dialect reports no headroom signal at all — silence is never read as exhaustion. |
| `pid` | The spawned process id for a CLI-launch worker, or `None` for a delegated launch that has no process of its own; `process_alive(pid)` is how a dead worker is told apart from a slow one. |

None of these fields are written by the worker. They exist so the orchestrator
can recover a run without reading the worker's own output — the run record
answers "where is it and is it alive," the manifest answers "what did it do."

### The record's second home

Every field above is worthless once the run ends, and all of it churns while the
run is alive, so the pointer lives under the crew home and is never committed.
The finished record is the opposite: durable evidence of how a plan was
implemented, which belongs with the plan. `reckon crew complete --run <id> --gate
<verdict> --commit <sha>` moves it — appending to the owning repository's
`docs/state/<project>/crew.json` first and deleting the pointer second, so an
interruption between the two leaves a recoverable pointer rather than a lost
record.

Promotion is also the only moment some measurements can still be taken, so state
them on the call: `--tests-added` and, when the node's scope was widened
mid-flight, `--scope-changed` — a scope-changed run measures neither the estimate
nor the worker and is excluded from calibration rather than averaged in. The
wall-clock, the agent configuration that ran the node, and the scoped diff's
changed lines are captured for you.

Promotion also ends the run's session as an addressable thing: deleting the
pointer takes `session_id` with it, so a followup arriving after completion has
no live run to resume. The session survives promotion in exactly one place — the
roster entry `capture_session` wrote in committed `crew.json` on that member's
first run. **A node dispatched with `--member` can therefore still be continued
after it lands; a node dispatched without one cannot.** That is why the roster is
the default dispatch path rather than an optimisation, and why the continuity
routing in `SKILL.md` distinguishes a followup (same member) from new scope (new
node).

That CLI continuity applies only when Reckon spawned the process. An in-harness
run has an attached task rather than a process id: answer or cancel it through
the host harness task/session. `reckon crew resume` and `reckon crew stop` refuse
that launch kind because they have no process to resume or signal.

Read that classification for every run at once with the `crew` MCP tool,
`view="live"` — it carries the same verdict and next action per run without
touching a worker. The CLI command below is the write-side twin: reach for it to
*repair* the record, not to look at it.

`reckon crew recover` is what a fresh session runs before assuming anything: it
classifies every remaining pointer as **running**, **completed-but-unpromoted**
(reporting the manifest path to promote from), or **abandoned**, and names the
next action for each. It repairs the record only — no worktree is force-removed,
no process is signalled, and nothing is promoted on its initiative.

## 7. The escape hatch

A vague "I'm stuck" wastes as much time as confused thrashing. A worker that
stops emits a report whose first line is `NEEDS-HELP:` followed by four required
fields:

```text
NEEDS-HELP: <one line naming the obstacle>
tried:         what was attempted and the observable result
options:       two or three concrete paths the worker can see
leaning:       which one, and why
cost-if-wrong: what must be redone if the wrong path is taken
```

Those four turn a plea into a decision brief the orchestrator can act on in one
turn. `crew.parse_needs_help` names any field that is missing, so an incomplete
brief is visible rather than merely unhelpful.

**Knowing when to stop matters as much as how.** Named triggers:

- the same command has failed twice with different fixes attempted;
- a decision the plan does not settle is required to proceed;
- the necessary change extends beyond the exclusive write scope;
- the gate's required evidence cannot be produced with the available tools or
  data;
- the time budget is spent with the gate still closed;
- **the work needs a workspace or resource the node was not given** — a
  compute-visible checkout a batch scheduler can reach, a longer budget for work
  that cannot be split, a staged dataset. Ask for it; the coordinator provisions
  infrastructure and will supply it in the same turn.

**Never shrink the measurement to fit the fence.** Reducing a grid, a sample, a
cohort or a resolution so the node finishes inside its budget changes what the node
demonstrates, and it does so invisibly — the manifest reports success against work
nobody asked for. Report the shortfall and request the resource instead. Partial
coverage honestly named ("3 of 6 shots, the rest not attempted") is a result;
full coverage at a quietly reduced fidelity is not.

The orchestrator answers it itself by default and escalates only genuinely
user-owned decisions such as scope trade-offs and irreversible choices. For a
CLI-launched run it resumes the **same** session with the advice:

```bash
reckon crew resume --run <run-id> --advice "<the answer>"
```

Resuming the same session is why session reuse is load-bearing rather than an
optimisation: the advice only makes sense to a worker that still remembers what
it tried. A fresh session would have to rebuild that context from nothing, and
would likely repeat the attempt that failed.

For an in-harness run, send the same advice through the attached harness
task/session instead. The CLI cannot recreate or terminate a process it did not
spawn.
