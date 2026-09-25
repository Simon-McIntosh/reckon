# Worker role digest: test
#
# Generated from ~/.agents/AGENTS.md (sha256 d01e79e203fb054dcdf19d98b268b7f96572b9320cd6235fe1a23bfd930577c2,
# 90028 bytes, ~22507 — the same policy holds for every
# role; this digest retains the sections below and withholds the
# coordinator-only delivery text.
# Regenerate with: python -m reckon.crew.worker_digest
# Edit the canonical file and regenerate; do not hand-edit this file.
#
# Sections retained, in canonical order (17):
#   ## Git Safety
#   ### Banned Commands
#   ### Anomaly Protocol
#   ### Commit Discipline
#   ### Pre-Commit Hook Policy
#   ### No Stray Clones (binding)
#   ### Naming & Comment Hygiene (binding)
#   #### Mandatory pre-stage naming check (binding)
#   ## Parallel Agent Safety
#   ### A Worker That Invents Its Own Manifest Status Slips The Review Guard
#   ## Bug and Failure Ownership
#   ## Compute Infrastructure (SDCC) — never heavy work on the login node (binding)
#   ### On the fleet allocation, run heavy work in place
#   ## A Worker's Process Ends With Its Turn
#   ## Test Visibility
#   ## Development Environment (binding, all repos)
#   ## Test Execution Protocol
#
## Git Safety
### Banned Commands

These commands have caused data loss in multi-agent sessions. They are
**banned unconditionally** — no model-tier or task-type exemption.

| Command | Ban scope |
|---------|-----------|
| `git stash` (any form: push, pop, apply, branch, create, store) | **Always banned** — commit instead |
| `git restore <path>` / `git checkout -- <path>` | Files the agent didn't author in-session |
| `git checkout <ref> -- <path>` | Files the agent didn't author in-session |
| `git clean -f` / `git clean -fd` | **Always banned** |
| `git reset --hard` | **Always banned** without user approval |
| `git add -A` / `git add .` / `git add *` | **Always banned** — stage specific paths only |
| `git revert <sha>` | **Never** revert another agent's commit without user authorisation |
| `git cherry-pick` | **Banned** unless user-approved |
### Anomaly Protocol

If a file looks wrong (content missing, prior edit is gone, unexpected
content):

1. **Stop** — do not try to restore to a known-good state.
2. Run `git status`, `git log --since="2 hours ago"`, `git reflog | head -20`.
3. Run `git log --since="2 hours ago" -- <suspect-file>`.
4. **Surface the anomaly to the user** before any destructive action.
5. Only proceed after user authorisation, with an explicit target SHA.
### Commit Discipline

- **Conventional commits:** `type(scope): description` or `type: description`,
  followed by a blank line and a BODY stating what changed and why. A
  bodiless commit fails review — the subject-only grammar is the format of
  the first line, not of the whole message. Dispatch templates must model
- **NEVER add AI attribution to ANY message — ZERO TOLERANCE, NO EXCEPTIONS.**
  This covers **every** message an agent authors, not just commits:

  | Surface | Banned content |
  |---|---|
  | Commit messages | `Co-Authored-By: Claude …`, `Co-authored-by: Copilot`, any AI trailer |
  | GitHub/GitLab **issues** | `🤖 Generated with [Claude Code]…`, "written by Claude", any AI footer |
  | **Pull/merge request** bodies + titles | same — no generated-with footer, no AI credit line |
  | PR/issue **comments**, review comments | same |
  | Plan/doc HTML, changelogs, release notes | same |

  No footer, no trailer, no "generated with", no tool self-attribution, no
  emoji robot credit — in **any** message, anywhere. Authorship is the
  user's; the tooling is not a co-author and never signs its own work.
- **No plan references in commits.** No phase labels, task IDs, or plan
  filenames in commit messages or PR titles.
### Pre-Commit Hook Policy

Pre-commit hooks (if present) MUST be **check-only** and MUST NOT modify
files. The pre-commit framework's stash/restore cycle is dangerous in
multi-agent environments. Run format/fix **before** staging:
**Scope the fixer to the paths you are staging — never `.`.** A whole-tree
`ruff check --fix .` / `ruff format .` is only harmless in a repo that is
already clean. In one carrying any backlog it rewrites files you did not touch,
and those edits land in your commit: measured 2026-08-26 in reckon, formatting
one changed module swept roughly 100 lines of unrelated rewrites (`timezone.utc`
→ `UTC`, import restructuring) across four files, from a style migration the
repo had not adopted. Two costs, both real — a reviewer can no longer see what
the commit *did*, and concurrent work in those files is now yours to merge.

**A broad relint is legitimate work, and it is its own commit.** When a tree
needs a sweep, land it alone: run the fixer repo-wide, change nothing else, use
a `style(lint):` or `chore(lint):` subject stating that behaviour is unchanged,
and verify the suite is green before and after. Never fold a sweep into a
feature, fix, or docs commit — mixing mechanical reformatting with intended
change is what makes both unreviewable. Sequence it when no peer holds
uncommitted work in the affected files, because a sweep touches many of them.

**Judge your own commit by delta, not by absolute count.** A file you edited may
carry pre-existing findings that are not yours to fix in passing; what you owe
is adding none:

```bash
T=$(mktemp -d); for f in $P; do install -D <(git show "HEAD:$f") "$T/$f"; done
C() { uv run ruff check --no-cache --output-format=concise "$@" | wc -l; }
echo "baseline $(C "$T")  yours $(C $P)"    # yours must not exceed baseline
rm -rf "$T"
```

Count with `--output-format=concise | wc -l`. The summary line ruff prints last
is the *fixable* tally rather than the finding count, so `tail -1` compares the
wrong number and reads as equal while the count moved.
### No Stray Clones (binding)

**Never `git clone` a repo into a sibling directory to "work in isolation."**
This silently strands work outside the canonical checkout and is invisible to
peer agents. Incident (2026-06-09, imas-codex): a prior session left
`imas-codex-clean-derived-parent-repair` (fully redundant) and
`imas-codex-catalog` (~1.1 MB of never-landed untracked work) as stray
sibling clones.
- For isolation, use a **git worktree** (`git worktree add`), never a fresh
  clone. Worktrees are tracked, share the object store, and are auto-audited.
### Naming & Comment Hygiene (binding)

**Never leak version numbers, RC tags, plan / issue / bug / ticket / PR
numbers, plan-document names, OR plan labels / stage names into source — not
in filenames, and not in function / class / variable / symbol names AND not
in comments or docstrings.**
"Plan labels / stage names" covers win-condition labels, deliverable IDs, and
phase / sprint / milestone identifiers (e.g. `W1`, `W2`, `D0`–`D5`, `S13`,
`M4`, `phase6`, `stage-2`, `rc18`, `plan32`). Code, symbols, and comments must
be understandable WITHOUT any plan, sprint, or release context, and must not
churn when a plan is deleted, a stage is renamed, or a version bumps. **Name
and comment by what the code DOES, never by which plan item it implements.**

- ❌ filenames: `build_sn_exemplar_catalog_v2.py`, `test_capability_gaps_plan32.py`,
  `migrate_rc18.py`, `phase6_cleanup.py`, `w1_compare.py`, `foo.py.bak`
- ❌ symbols: `def evaluate_w1(...)`, `def run_d3_ablation(...)`,
  `W1_verdict = …`, `class S13Pipeline`
- ❌ comments/docstrings: `# W1 = dynamics beat statics`, `# D0 substrate`,
  `# phase-2 gate`, `# BUG 9 fix`, `# fixes #272`, `# workaround for JIRA-1234`,
  `"""Handles the RC-2 desync case."""`
- ✅ filenames: `build_exemplar_catalog.py`, `test_node_category_eligibility.py`,
  `migrate_physical_base.py`, `arm_compare.py`
- ✅ symbols: `def compare_arms(...)`, `favours_dynamics`,
  `def conditioning_ablation(...)`
- ✅ comments: describe the mechanism — `# paired bootstrap: dynamics vs baseline`
- **Gate / verdict IDs are plan labels.** Pre-declared gate names (`T-B1`,
  `G4a`, `Q1`, `T-E3`, …) are deliverable IDs: functions, variables, and
  tolerance constants in an eval harness must be named by WHAT THEY MEASURE
  (`_smooth_convergence_gate`, `SMOOTH_PSI_TOL`), never `_te1` / `TE1_PSI_TOL`.
  The gate ID may appear ONLY as the emitted artifact's verdict key / log
  label (output DATA, like a checkpoint path), with the ID→mechanism glossary
  documented once at the emission point (module docstring).
- **Matching an existing in-file pattern is not an exemption.** If a file
  already contains plan-labelled symbols (`_tb1`, `_w2_eval`, …), extending
  the pattern is a NEW violation — rename the existing symbols in place as
  you pass through, per the scrub rule below.
- **Comments and docstrings are code, not a changelog.** A transient
  bug / issue / ticket / PR / RCA-cause reference (`BUG 9`, `#272`, `RC-2`,
  `JIRA-1234`, `see PR #88`) in a comment or docstring is as banned as one in a
  symbol name — it rots the moment the tracker entry is closed and means
  nothing to a reader without tracker access. State WHAT the code does and WHY
  in mechanism terms (`# re-derive the edge because the scalar mirror can
  desync`), never WHICH bug motivated it. The tracker/commit message owns the
  bug↔code link; the source must stand alone. Scrub such references in place in
  your own focused commit when you pass through them.
- **No author / role attribution in source — comments, docstrings, class /
  function / file names alike.** `(lead directive)`, `the lead asked for`,
  `user mandate`, `orchestrator adjudication`, reviewer or agent names — all
  banned wherever plan-stage names are banned. Provenance lives in commit
  messages and plan documents; source states the RULE or MECHANISM itself
  (`# every circuit shares one interaction matrix so all dψ/dt terms are
  carried`), never who asked for it. A design rule that needs its author cited
  to be credible is under-explained — write the physics/engineering reason
  instead. Scrub existing attributions in place as you pass through, same as
  ticket references.
- No `_v2`/`_new`/`_final`/`_old`/`.bak` suffixes — git is the version store.
  When superseding a file, replace it in place (one commit), don't keep both.
  **A parallel `*_v2` MODULE FAMILY beside the originals (e.g. `model.py` +
  `model_v2.py` + `train_v2.py` + `dataset_v2.py`) is the worst form of this** —
  it doubles the surface, hides which file is canonical, and churns on every
  architecture bump. Decide WHEN YOU CREATE THE SECOND FILE (not "later" —
  "later" is exactly how a `_v2` family accretes): if the new design SUPERSEDES
  the old → replace the contents in place and delete the dead file (git holds the
  history); if both genuinely COEXIST (the new one layers on / extends the old)
  → name the new file by the CAPABILITY that distinguishes it
  (`signal_conditioned_dynamics.py`), never by a version number. The version
  never appears in a source path.
- The plan/issue tracker owns the stage↔code mapping; a commit message or a
  plan's own HTML may reference `W1`/`D0`, but the CODE must stand alone. When
  you notice an existing violation, rename it in place **in its own focused
  commit** as you pass through — but never mid-flight on names a running/queued
  job's code or checkpoint paths resolve through. (A data/checkpoint dir that
  encodes a version — e.g. an existing run's output dir — is an *artifact*:
  leave it, and rename only source.)
#### Mandatory pre-stage naming check (binding)

**Noticing is exactly what fails**, so the ban is paired with checks bound to a
moment that always happens. **Touching a file makes its name and its comments
your responsibility.** Run all three over the EXACT paths you are about to
stage, BEFORE `git add`:

```bash
# 1. banned labels in the PATHS
printf '%s\n' <paths> | grep -Ein \
  'phase[-_ ]?[0-9]|stage[-_ ]?[0-9]|plan[-_ ]?[0-9]|sprint[-_ ]?[0-9]|(^|[^a-z])(rc|v)[0-9]+|_(v[0-9]+|new|old|final|bak)\b|\b[A-Z][0-9]+[a-z]?\b'

# 2a. banned label WORDS — case-INsensitive (catches Bug 5, Phase 2, Wave 2)
grep -Ein 'bug[- ]?[0-9]|\bplan [0-9]|§[0-9]|JIRA-|#[0-9]{2,}|acceptance #|\
\b(phase|stage|step|track|wave|sprint|milestone|lever|gap)[-_ ]?[0-9A-Z][0-9.]*\b' <paths>

# 2b. bare deliverable IDs — case-SENSITIVE, and it must stay that way
grep -En '\b[A-Z][0-9]+[A-Za-z]?\b|\bf-[a-z]{3,}-?[0-9]*\b' <paths>

# 3. banned CHANGELOG PROSE — check 2 does NOT find any of this
grep -Ein "in a prior incident|before (this|the) fix|after (this|the) fix|\
used to (be|return|do|call)|we (used to|previously)|\
the (old|previous|original) (code|behaviour|version|implementation)|the plan's" <paths>
```

**Check 4 is a question, not a regex:** whenever a comment, docstring or symbol
cites an anchor — a file, a section, a numbered principle — open it and confirm
it exists. A pointer to something renamed or deleted is worse than none.

Rewrite to the MECHANISM, which is usually the stronger sentence. Why each check
is shaped this way, the measured incidents behind them, and the four carve-outs
where a flagged hit is CORRECT and must be kept:
`~/.agents/references/naming-checks.md` — read it before deciding a hit is a
false positive.
## Parallel Agent Safety

Every background worker gets its own detached worktree
(`worktree_fleet.py` creates and conservatively cleans them); worktree
isolation now carries most of the burden the shared-checkout fleet rules
existed for. What remains binding:

- One worker, one worktree, one exclusive write scope. No two concurrent
  workers write the same file even across worktrees — merging is the
  orchestrator's job, sequentially, after auditing each manifest and
  `git show --stat` against the declared scope.
- Workers commit locally (conventional subject AND a body), never push the
  primary branch, never merge/rebase/stash, and stage explicit paths only.
- Durable delivery: every worker writes its manifest to an
  orchestrator-named file; the reply is a convenience, the file is the
  delivery. Recovery order on a silent worker: manifest file → on-disk
  logs → `reckon crew resume --run <id> --advice "<answer>"` → only then
  redispatch.
- **Never message a CLI-launched worker as a peer session.** It is listed
  as `interactive` and the send returns success, and it has delivered
  nothing in 3,949 worker streams on this workstation — against a positive
  control of 11,939 deliveries in 244 interactive transcripts. The
  recipient is a headless `-p` process with no user, so the approval its
  permission mode requires can never be given and the message expires
  silently. Resume answers a worker whose turn has ended; a worker still
  mid-turn has no channel at all, so plan the node rather than expect to
  steer it. Rationale and measurement:
  `~/Code/reckon/docs/research/messages-to-workers-never-arrive.html`.
- The orchestrator owns merges, pushes, plan/index state, and cleanup.
  Cleanup never forces — a refused removal is a visible blocker with a
  recoverable path.
- The canonical dispatch contract (embed it verbatim in worker prompts)
  lives in `~/.claude/skills/reckon-build/references/sprint-orchestration.md`
  §6; do not restate plan content in prompts.
- Session audits: `git stash list` at start (stashes are user-owned);
  `git worktree list` before ending — no orphaned session trees left
  behind.
### A Worker That Invents Its Own Manifest Status Slips The Review Guard

**A manifest whose `status:` is outside the recognised set classifies as nothing, and a
run that classifies as nothing is never held for review.** The guard is not bypassed
loudly — the run simply never enters the state the guard inspects.

Measured 2026-09-22 on imas-efit, **twice in one session, by different workers on
different nodes**: `delivered-awaiting-coordinator-adjudication` and
`ready-for-review`. Both were written by workers behaving *well* — each had finished,
committed, and was deliberately signalling that the coordinator owned the next step.
Both surfaced as `unreadable` rather than as complete, and both would have promoted
without a review had the coordinator taken the status at face value. This sits beside
an earlier measurement on the same workstation of 11 promoted runs in 34 exempting
themselves the same way.

The recognised values are `complete`, `blocked` and `failed`. Anything else, however
descriptive, is a null.

- **Repair the status; never promote through the gap.** Rewrite `status:` to the
  recognised value the run actually reached, keep the as-delivered manifest beside it
  (`manifest.md.asdelivered`), and say in a comment what it read and why it changed.
  The run then reclassifies within a poll and the review guard fires normally.
- **Read a run's `status:` line before believing any classifier that calls it
  unreadable.** The phase word describes the *record*, not the work: both runs above
  had committed, clean worktrees and complete deliverables sitting behind a one-line
  defect.
- **The prompt is where this is cheapest to fix.** A worker signalling "done, your
  move" needs somewhere to put that intent which is not the status field — the
  `wait_condition` and `checkpoint` fields already carry it. If workers keep reaching
  for the status field to say it, the contract is under-specified, not the workers
  careless.
## Bug and Failure Ownership

**Fix bugs you find — do not walk past them.** The pattern of ignoring test
failures or code defects because they are "not mine" is an anti-pattern that
degrades project health. If you discover a bug, a test failure, or a
regression during your work, you are expected to fix it — not log it and
move on.

**Ownership rule:** before fixing, check whether the affected files are
already claimed by a concurrent agent:

```bash
git status                              # are the files dirty (in-flight work)?
git log --since="2 hours ago" -- <path> # recent commits from another agent?
```

- **Files are clean and no recent agent commits →** you own it. Fix it now.
- **Files are dirty or recently committed by another agent →** the other
  agent is actively working on them. Flag the issue to the orchestrator
  and continue your own work.

**Never bisect to assign blame.** Git-bisect workflows in multi-agent
sessions are the primary trigger for dangerous `git stash` and
`git checkout` patterns. They are slow, they contaminate the worktree,
and they answer the wrong question ("who broke it?") instead of the right
one ("is it fixed?"). If a test is failing and the code is not claimed
by another agent, just fix it.

**Scope expansion:** if fixing the bug requires editing files outside your
assigned write scope, request a scope extension from the orchestrator
rather than touching out-of-scope files unilaterally.
## Compute Infrastructure (SDCC) — never heavy work on the login node (binding)

**The login node runs editors, git, reckon, file reads and unit tests that finish
in seconds. Everything else runs on SLURM compute, always.** Heavy means any of:
a test suite that opens real corpus data, a statistics or census pass over shots,
checkpoint scoring or evaluation, training, benchmarks, corpus or archive scans,
video or figure rendering over many frames, builds, and any command expected to
run longer than about ten seconds of CPU. CPU-only work goes to a `*_debug`
partition (`--time <= 01:00:00`; a larger value pends forever on
`PartitionTimeLimit`, so split longer work into one-hour chunks); GPU work goes
to `betelgeuse` under the reservation rules in the reference. Set
`export TMPDIR=/tmp` on the node, never `--mem=0`, and capture the whole run to a
log file once.

```bash
# the shape every heavy gate takes — one srun, one log, read the log afterwards
srun --partition=<name>_debug --time=00:59:00 --cpus-per-task=4 --mem=32G \
  bash -lc 'export TMPDIR=/tmp; cd <worktree>; \
    UV_PROJECT_ENVIRONMENT=<main-checkout>/.venv PYTHONPATH=$PWD \
    uv run --no-sync pytest -p no:cacheprovider <targets>' > <log> 2>&1; echo EXIT=$?
```

Measured 2026-09-06 on the imas-ambix login node: load average 16.9 with two
worker nodes running a 472-shot statistics pass and a checkpoint window census
there, niced to 17 percent CPU and starving every other session on the machine.
The lead's directive that day is the rule above, without exceptions.

**Obligations that follow.** A coordinator names the partition in the done-when
of every node whose gate is heavy, so the worker never has to decide; a worker
that finds its gate heavy and unnamed runs it on a debug partition anyway and
says so in the manifest; a manifest whose heavy gate ran on the login node is a
scope defect, and the coordinator stops the run and redispatches rather than
accepting the evidence. Partition reference, the H200 reservation and launch
recipes: `~/.agents/references/sdcc.md` and the `compile-on-sdcc` /
`run-on-sdcc` skills. `module load` mutates one process, so all
module-dependent work runs in ONE persistent shell.
### On the fleet allocation, run heavy work in place

**A session whose shell has `SLURM_JOB_ID` set is already on compute. It runs
its heavy gates directly and never submits them to a `*_debug` partition.** The
fleet allocation (`~/.local/state/fleet/record.json`) holds one node for every
orchestrator and its workers. The debug-partition recipe above is for the login
node only. From the fleet node it queues work behind other users for no gain. The
allocation is **28 CPUs and 120 GB**, not the whole cluster, and every
orchestrator and worker shares it. Size parallel work to leave room for them:
about 8 to 12 pytest processes, not one per core.

- **Temporary files go on node-local `/tmp`, never on GPFS scratch.** `/tmp` on a
  compute node is a local disk (232 GB on clu-2058). It suits the small-file
  churn tests do: git repos, fixtures, and many tiny writes. GPFS scratch would
  bring back the token traffic the login node suffers from. Use `export TMPDIR=/tmp`,
  which the fleet already sets.
- **Anything a later reader needs goes on GPFS.** That covers gate logs, evidence
  and manifests. Node-local `/tmp`, and the session scratchpad under it, is
  invisible from login and gone when the allocation ends. Write logs to a named
  path under the repository or `~/.cache/<repo>-<topic>/`.
- **Every parallel test run gets a separate basetemp for each process.**
  Concurrent pytest processes share `/tmp/pytest-of-$USER` and delete each other's
  basetemp, because pytest keeps only three. Measured 2026-09-24: 16 per-file
  processes turned passing tests red until each got `--basetemp`. `pytest-xdist`
  (`-n 8`) isolates its workers itself and is the preferred form.
- **Every suite run has a per-test bound.** One hung test stalls a serial run
  indefinitely, and in a Monitor it looks like slowness. Measured 2026-09-24:
  reckon's suite was 14% through after 30 minutes, sitting on a `watch()` loop
  that never returned, because `pytest-timeout` was not installed. A repository
  whose suite takes more than a minute declares `pytest-timeout` and a default
  `timeout` in its pytest config.
- **GitHub SSH on port 22 is refused from compute nodes.** Push with
  `GIT_SSH_COMMAND="ssh -o Hostname=ssh.github.com -o Port=443"`.
## A Worker's Process Ends With Its Turn

A dispatched worker in print mode gets no turn after its last one, so a
background task it starts is not something it can wait across. Measured
2026-09-03 on two nodes of mine, one of each variant: one was killed at a
600-second ceiling (`Background tasks still running after 600s; terminating`)
while its own terminal record read *"continuing to wait for pool activity"*;
the other exited with **empty stderr after 56 turns** whose final text was
*"waiting for the background suite run to complete before finalizing the
manifest"*. Nothing terminated the second — its process simply ended while it
expected another turn. Both were one turn short of a manifest, and both
displayed as a vanished process.

**Write the manifest with what you have, then start the wait.** Never defer it
until the wait returns. A manifest naming the work done and the check still
running is recoverable; a deferred one is indistinguishable from a crash.

**When a node's measure needs a long suite or a long pipeline drain, say so in
the done-when and keep it in the foreground under a bounded budget.** A
done-when that asks for a full-suite result invites the worker to background it
and lose everything after it, which is the orchestrator's error, not the
worker's — the requirement created the exposure.

**Recovery is resume, not redispatch.** Both signals sit in the run directory:
the ceiling line in `stderr.log`, and the worker's own terminal text saying it
was waiting. Where the live pointer survives, `crew resume --advice` continues a
session holding all its turns; a redispatch discards them and a promotion
destroys the resume path. Check the worktree for committed *and* uncommitted
work first — the dangerous case is uncommitted work with no commit behind it,
because a reclaimed worktree takes it outright with nothing to recover from.
## Test Visibility

**Prefer visible failures over hidden tests.** When a test fails, leave it
enabled so the failure directs engineering effort. Disabling a test hides
the problem — failing tests in CI summaries are actionable signals.

| Mechanism | CTest output | Signal value |
|-----------|-------------|--------------|
| DISABLED TRUE | "Not Run" — invisible | **Zero** — nobody knows it exists |
| Test runs and FAILS | "FAILED" in summary | **High** — directs effort |
| Test runs and PASSES | "PASSED" | **High** — confirms recovery |

In C++/Fortran codebases with slow builds, every test run costs real time.
Hidden failures waste those runs. Use `SKIP_RETURN_CODE 77` for the
"can't run yet" case instead of DISABLED.

Project-specific test visibility rules (which categories may be disabled
and under what conditions) belong in the repo-level `AGENTS.md`.
## Development Environment (binding, all repos)

**Develop against the repo's single project virtualenv at its root, and keep it
current.** One `.venv` per repository. Agents use it, sync it, and update it —
keeping it in step with `pyproject.toml` is part of doing the work. The two
things that are banned are creating a *second* environment and changing an
environment in a way that leaves no trace in the project files.

- **Syncing the root environment is expected and required, not a setup step to
  avoid.** `uv sync`, and a plain `uv run <cmd>` (which syncs first), against the
  repo's own root `.venv` are normal workflow. If the root environment is stale,
  incomplete, or absent, bring it up to date — that is your job, not a blocker to
  hand back. Report only when the sync itself fails, and report what it printed.
- **Every dependency change must be durable and pyproject-backed.** Add and
  remove dependencies with `uv add` / `uv remove`, or by editing
  `pyproject.toml`, so the change lands in `pyproject.toml` and `uv.lock`,
  survives the next sync, reaches every other checkout and CI — then commit both
  files. **Never `pip install` into the environment.** An imperative install is
  invisible to everyone else, unreproducible, and deleted by the next `uv sync`;
  if you needed a package to make something work, the project needs it declared.
  The test is simple: after `rm -rf .venv && uv sync`, does your work still run?
- **One environment per repository — a worktree must never build its own.** This
  is the expensive failure mode: measured on imas-codex, one project environment
  is **69,826 filesystem entries and 1.76 GiB** on GPFS, and a worker fleet each
  creating its own produced a **180,186-file** storage alert in a single day
  (2026-08-17), 98% of it from three such copies. The cost is duplication, not
  syncing. Point the worktree at the main checkout's environment:

  ```bash
  # inside a detached worktree of <repo>
  UV_PROJECT_ENVIRONMENT=~/Code/<repo>/.venv PYTHONPATH="$PWD" \
    uv run --no-sync pytest <targets>
  ```

  `--no-sync` is the default *in a worktree* because that environment is shared
  with the main checkout and any concurrent workers — an incidental sync there
  mutates a resource under peers who did not ask for it. When your work genuinely
  changes dependencies, make the change durable in `pyproject.toml` first, then
  drop `--no-sync` deliberately and say so in your report, so an orchestrator can
  serialize it against other workers.

  An inherited `VIRTUAL_ENV` does **not** achieve this — uv warns that it does
  not match the project environment path and creates `.venv` anyway.
  `UV_CACHE_DIR` and `TMPDIR` are not substitutes either: the first moves only
  the shared download cache, the second only conventional temp files. A worker
  sandbox that makes the uv cache read-only breaks `uv run` before it reaches the
  command, so an execution task needs write access to both the cache and the
  environment.

- **A worktree that syncs the shared environment repoints it at itself, and
removing that worktree breaks the environment for everyone.** `uv sync` (without
`--no-sync`) from a worktree rewrites the editable install in whatever
environment it resolves — including the shared one reached through the `.venv`
symlink. Measured 2026-09-03: `_editable_impl_<project>.pth` in the main
checkout's environment pointed at a worktree that had since been removed, so
`import <project>` resolved nowhere, all eleven console scripts in
`.venv/bin` carried that worktree's interpreter in their exec line, and every
worker needed `PYTHONPATH` to run anything. `python -c "import <project>"` still
passed, because `-c` puts the working directory on the path — so the breakage
hides from the obvious check and shows up only through a console script.

Diagnose with the check that does not have cwd on the path, and repair by
syncing the main checkout, which rewrites both the `.pth` and the scripts:

```bash
.venv/bin/<console-script> --version          # fails where `python -c` passes
cat .venv/lib/python*/site-packages/_editable_impl_*.pth
grep -l reckon-worktrees .venv/bin/*          # scripts holding a foreign interpreter
uv sync                                       # from the MAIN checkout only
```

This is the concrete cost behind `--no-sync` being the default in a worktree:
the flag protects the shared environment's identity, not just its package
versions.

**The dangerous case is the worktree that still exists: nothing breaks, and the
environment quietly serves stale source.** The diagnosis above finds the loud
failure — a worktree since removed, an import that resolves nowhere. While that
worktree is still on disk the console script runs, `--version` answers, and every
command behaves normally *while resolving that worktree's revision of the code*.
Measured 2026-09-10: a peer session's worktree had repointed the shared
environment an hour after a change landed on the main branch, and the tool went
on composing its output from a tree that predated the change. The commit was
pushed, reachable, and present in the file on disk; the running program did not
contain it, and nothing reported the difference.

So **a change is not in effect until the running code carries it**, which is a
different fact from the merge check and needs its own instrument:

```bash
PY=<main-checkout>/.venv/bin/python
$PY -c "import <project>; print(<project>.__file__)"        # which tree actually serves
$PY -c "import <module> as m; print(hasattr(m, '<symbol the change introduced>'))"
```

A `__file__` under the worktree root is the tell. Repair by syncing the main
checkout as above — but **not while a peer's run is live in that worktree**,
because the sync pulls the editable pointer out from under a working process.
Until it can be repaired, shadow it per command with
`PYTHONPATH=<main-checkout>`, and require the marker to read `True` before
trusting anything the tool emits. This is the same shape as the git-notes trap
and the dropped-merge check: a true statement — the commit is on the branch —
standing in for the one that was needed.

**Provision the worktree at creation; the rule above does not enforce itself.**
  Every instruction so far depends on the worker remembering to set
  `UV_PROJECT_ENVIRONMENT` on each command, and a worker that forgets once builds
  the second environment this section exists to prevent. Bind provisioning to the
  moment that always happens — worktree creation — the way the naming check binds
  to `git add`. Immediately after creating or dispatching into a worktree, before
  the worker gets far, run:

  ```bash
  W=<worktree-path>; ROOT=<main-checkout>
  ln -s "$ROOT/.venv" "$W/.venv"          # a forgetful `uv run` now reuses the common stack
  ln -sfn "$ROOT/.env" "$W/.env"          # gitignored: a fresh worktree has no credentials
  for f in <repo's gitignored generated files>; do
    mkdir -p "$W/$(dirname $f)"; cp -n "$ROOT/$f" "$W/$f"; done
  ```

  **Add a bare `.venv` to the repo's `.gitignore`, not only `.venv/`.** A
  trailing-slash pattern matches directories only, so the main checkout's real
  `.venv` is ignored while this symlink is not — and every provisioned worktree
  then reports one untracked entry. Measured on imas-codex: 21 worktrees each
  showing exactly one, which reads as a dirty fleet until someone opens one. An
  identical dirt count across unrelated trees is the tell that it is
  provisioning, not work.

  The `.venv` symlink is the load-bearing line: it makes the common stack the
  path of least resistance rather than a rule to recall. It does not retire
  `--no-sync` — a plain `uv run` through the symlink syncs the ROOT environment,
  which is the shared-resource hazard named above, so the flag still governs
  whether you mutate it. Never symlink in the other direction: the root `.venv`
  must stay a real directory.

  **Verify rather than assume, and sample late.** A worktree measured seconds
  after dispatch has not finished being provisioned, and a reading taken there
  cannot support a claim about what dispatch does or does not supply. Sandbox tier
  decides what a worker can self-provision: a read-only investigate worktree
  cannot run a build step at all, so pre-placement is mandatory there and merely
  belt-and-braces for execution-capable roles.

  ```bash
  # every worktree, before believing anything about them
  for w in <worktree-root>/*/*; do
    printf '%-44s venv=%s env=%s\n' "$w" \
      "$(readlink $w/.venv 2>/dev/null || (test -d $w/.venv && echo REAL-DIR-BAD) || echo none)" \
      "$(test -e $w/.env && echo ok || echo missing)"
  done
  ```

  A `REAL-DIR-BAD` result is the failure this section exists to catch: stop, and
  remove it before it multiplies across the fleet.

- **Repo `AGENTS.md` files must match this policy.** Text forbidding `uv sync`,
  or telling agents to stop when the environment needs updating, is wrong and
  must be corrected — as is text instructing a per-worktree environment. Record
  the root-environment recipe and the worktree reuse recipe above instead.

**`/tmp` is a scarce shared allocation, not a scratch dump.** Keep what lands
there small and short-lived. Never place a virtualenv, a package cache, or a
multi-gigabyte artifact in it. For one-shot agent runs prefer disabling
incremental tool caches over relocating them (`ruff --no-cache`,
`pytest -p no:cacheprovider`, `mypy --no-incremental`), because moving a cache
to `/tmp` just shifts inode pressure onto a smaller filesystem. Session scratch
belongs in the harness-provided scratchpad directory when one is offered.
## Test Execution Protocol

**Select the gate from what changed.** A test that cannot reach the edited code
tells you nothing about it, and running it anyway costs wall clock on every node
and contends with every peer. Derive the gate from the node's declared write
paths: the test files that cover them, plus any file that imports what changed.
Nothing else.

- **No full suite per node.** One integration verification per wave, at the
  merged head, is where a suite-wide result is informative — it measures the
  merge rather than one worktree, which is the only thing a worktree run could
  not already tell you. Measured in reckon 2026-09-02: a focused gate ran 189
  tests in 27 s against 700–900 s for the suite, thirty times cheaper for the
  same change.
- **Slow surfaces are opt-in.** Browser-driven, GPU-driven and network tests run
  only when the change reaches that surface. They also leak: 35 headless Chrome
  processes were found orphaned at 2.5 h old, holding pytest profile dirs.
  Reap by profile path and age, never by parent pid and never by a `pkill`
  pattern — resolve the owning pids first, per Killing Processes On A Shared
  Login Node above.
- **Judge by delta against a stated base, never by absolute green.** A shared
  checkout carries peer work in flight, so an absolute count measures the fleet.
  Record the base revision, its failure ids, and yours; added failures is the
  number that matters, and zero added against a red base is a pass.
- **If nothing the tests cover has changed, do not run them** — read the last
  log. Re-running to get different output formatting is never a reason.

Mechanics: capture the whole run to a file ONCE (`> log 2>&1; echo EXIT=$?`),
then read selectively — the summary is the last line, `grep ^FAILED` lists
failures, `-B2 -A10` pulls a traceback. Never pipe test output through
tail/grep/head. One execution method per investigation; repeated runs are
legitimate only as an explicit stability measurement, one log each. Language and
repo specifics — pytest flags, which files cover which sources, ctest labels,
SLURM lanes — live in each repo's `AGENTS.md`.
