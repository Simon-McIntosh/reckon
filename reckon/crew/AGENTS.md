# Agent Guidelines — reckon/crew

> This file governs the `reckon/crew/` sub-tree: how worker fleets are
> dispatched, scoped, watched, and routed, and the CLI surface that runs them.
> It is loaded automatically when work happens inside this directory; repo-wide
> rules live in the root `AGENTS.md`.

### Crew command surface

Crew operations use one backend-agnostic CLI surface. Commands emit JSON by
default (`--pretty` only changes formatting), and run records remain available
to later orchestrator sessions. The live plan supplies semantic context; the
dispatch carries only the node definition and its scope, time, evidence and
delivery fences.

| Operation | Command | Contract |
|---|---|---|
| Pre-flight | `reckon crew preflight --project <project> [--role <role>] [--backend <name>] [--purpose dispatch\|resume]` | Decides per backend whether a wave may open, from the budget signal earlier runs already recorded — so it spends none of the resource it measures. Exits 3 when any backend is held, naming its utilisation and reset time; a backend reporting no headroom is never held. A `dispatch` keeps back `budget.resume_reserve_pct` so a stuck worker can still be answered; a `resume` may spend it. |
| Dispatch | `reckon crew dispatch --project <project> --plan <slug> --section <section> --role <role> --node <node> --goal "<one deliverable>" --done-when "<measure>" --write-path <path> --time-budget <duration> --session <session> [--manifest <absolute-durable-path>]` | Validates the node, derives peer scopes from live pointers in the same project and repository, then atomically creates the detached worktree plus either a launched CLI run or an in-harness dispatch directive. A containing or contained live path claim is refused before worktree creation with `scope-conflict` at exit 7, naming its owning run; `--peer` only supplements peers that are not live yet. A process-launching dispatch is refused before worktree creation with `watcher-required` at exit 8 when the project has no producer, and equally when the named `--session` has no *delivering* follower of its own — a seat is project-global while wake delivery is session-local, so a peer's producer never admits your dispatch. The refusal names the exact command to arm. `--no-watch` is the explicit synchronous exception and records the waived arming command, watcher liveness and session attachment on the live and promoted run records. An in-harness result only prepares a directive, so arm the watcher before the host launches and attaches that task. The manifest defaults to `<config-home>/crew/runs/<run-id>/manifest.md`. Every successful payload also returns `watch.arming_line`, `watch.attach_line`, `watch.watcher_live` and `watch.session_attached` — the last being the only one that says whether *this* session will hear the run finish. Other dispatch exits are 0 on success, 1 for a malformed request or any refusal without a code of its own, 2 when the node is not dispatchable, 3 for a budget hold, 4 when the named plan section is unavailable at the base revision, 5 for a capability refusal, 6 when terminal live pointers exceeded `fences.unreconciled_run_grace`, and 9 when the named roster member already holds a live run. Every refusal, coded or not, answers with a JSON document on stdout carrying `error` and `detail`, so a caller reading the documented channel never has to infer a refusal from silence. The last refusal names every reconciliation command; `--allow-unreconciled-runs` is the explicit exception and stores the waived rows on the new run. Repeat `--write-path` for the complete exclusive scope; use `--dry-run` to validate a call. Add `--member <id>` to reuse a roster session; a non-terminal live pointer for that member is refused before worktree creation with `member-in-flight` at exit 9, naming the owning run. |
| Attach | `reckon crew attach --run <run-id> --task <harness-task-id>` | Binds an in-harness task to the prepared run record returned by dispatch. CLI-backed runs are already bound when launched and do not use this step. |
| Observe | `reckon crew observe --run <run-id> [--project <project>]` | Folds the event stream, manifest presence and process liveness into the durable run record, including the phase, session id and any backend budget signal. An absent budget signal remains `unknown`; it is never evidence of exhaustion. |
| Watch | `reckon crew watch --project <project> [--stall-window <duration>] [--width <cols>] [--theme dark\|light] [--no-color]` | Claims the project's single blocking producer, turning pointer changes into transitions on a durable per-project stream of transition records. Dispatch arms it detached, so a coordinator rarely runs it by hand. `--once` returns after a single event and releases the seat. A concurrent invocation reports the live watcher and exits; an unlocked stale record is reclaimed. Read-only liveness never takes its lock, so observing cannot deny an arming. |
| Follow | `reckon crew follow --project <project> [--session <session>] [--attention] [--run <id>] [--json] [--width <cols>] [--theme dark\|light] [--no-color]` | The session's own delivery, and the half a producer cannot supply: it registers the named session as attached and streams only that session's runs, so several coordinator sessions sharing one project each hear their own fleet. It reports every transition, starts included, and opens with one line per live run — a filter that legitimately matches nothing is an empty pane, which reads exactly like a follower that never started. The stream carries worker transitions and fleet posture only; follower status is not fleet state, and nothing goes to stderr. Each line is a fixed grid — clock, node, `from → to`, then the model and effort that ran it from the configuration persisted at dispatch, and the fleet counts right-aligned on the margin — so the pane is read down a column rather than across a line. The counts are `N working · N blocked · N unpromoted` after that transition, folded through the same function the seat's own ticker uses; the three buckets partition it, so a blocked or delivered run is not counted as working. A state a reader must act on carries one clause saying why, truncated to the room the grid leaves: no line ever wraps, because the pane shows about eight and a wrapped row costs a quarter of them. Neither the width nor the colour can be detected — the pane is a pipe, so `isatty` is false and `COLUMNS` unset, and the conventional terminal probe would disable both in exactly the place they are wanted — so both are stated. The defaults suit a wide light pane, which is why the arming line stays bare. `--attention` is opt-in for a caller that wants only the actionable states. One bare command by design — filtering lives inside it because a shell pipeline can withhold every line until exit, match the fleet summary on every line, or hide a refusal behind `|| true`. It produces lines and does not exit: it waits for a producer rather than refusing, survives a drained wave to cover the whole session, re-derives state on re-attach so a reconnect repeats nothing, and puts status and refusals on stderr so stdout is one notification per transition. Registration records how its lines are consumed, measured live from the follower's own descriptor and traced through any pipe to the process that ends the chain; a follower whose lines end in a file nothing reads until exit is registered as not delivering, which is what dispatch refuses on. A second follower for one session streams read-only while the first holds the registration and takes it over within a poll once that holder goes, so releasing never leaves lines arriving at an unregistered reader. |
| Drain | `reckon crew drain --project <project> [--leave <run-id>=handed-off\|still-working]` | Derives the closure count from current live pointers. Every pointer counts unless it carries a valid closed-set disposition; `still-working` expires when the run turns terminal, and promotion removes the pointer from the count. |
| Resume | `reckon crew resume --run <run-id> --advice "<answer>"` | Answers a structured `NEEDS-HELP:` report in the same CLI-spawned worker session so its prior context is retained. Use `--print-only` to inspect the resume invocation without launching it. Continue an in-harness run through its attached host task/session instead. |
| Stop | `reckon crew stop --run <run-id>` | Stops a CLI-spawned run's process group and records the stopped phase. Cancel an in-harness run through its attached host task; it has no Reckon-spawned process to signal. |
| List | `reckon crew list` | Lists all live run pointers with node, plan, backend, phase, worktree and manifest path so a fresh orchestrator session can recover ownership. |
| Complete | `reckon crew complete --run <run-id> --gate passed\|failed\|not-run --commit <sha> [--tests-added N] [--scope-changed]` | Promotes the finished run into the owning repository's committed ledger, then deletes the pointer — in that order, so an interruption is recoverable. Records the calibration inputs no later reader can reconstruct: dispatch and completion stamps, the agent configuration that ran the node, the scoped diff's changed lines, tests added, the gate verdict, and `--scope-changed` when the node's scope was widened mid-flight. |
| Recover | `reckon crew recover [--project <project>]` | Classifies every live pointer an interrupted orchestrator left as running, completed-but-unpromoted (reporting its manifest path), or abandoned, and repairs the record only — it never launches or resumes work, promotes a run, force-removes a worktree, or signals a process. |
| Roster | `reckon crew member add --project <project> --member <id> --harness <backend> [--session <id>]` · `reckon crew member list --project <project>` | The project's committed team. A member registered with no session captures one from its first run and reuses it for every later node and every escape-hatch resumption. |
| Ledger | `reckon crew ledger --project <project> [--view summary\|records]` | Reads the committed record of how this project's plans were implemented: roster, gate outcomes, and measured worker-time per plan against its declared effort with the spread. |
| Repair completion | `reckon crew repair-completion --project <project> [--write]` | Repairs completion measurements: it reports the re-derived values by default and persists them only with `--write`. Its name deliberately states the measurements it touches. |

### Recovering a run blocked by a provider refusal

A per-request provider refusal kills a worker's turn without killing its
session: the run reads `blocked`, but its worktree is untouched and its
session transcript is still on disk. A live pointer's `session_id` reading
empty means only that `observe` has not run yet — not that the run is
unresumable, since `resume_plan` recovers a missing id from the stream itself.
Two coordinators read that empty field as a verdict on the same day: one
promoted five recoverable runs, destroying their resume paths; the other left
a run blocked for five hours and forty-two minutes after its refusal had
already lifted. The order recorded for this failure was not applied the
second time it was needed, four hours after the first.

**The order:** inspect the worktree for committed and uncommitted work, resume
the session, and reconcile (promote or discard) only once resume is
impossible — never the reverse, because promotion deletes the pointer that a
resume depends on and cannot be undone by resuming afterward. Promoting first
to "tidy the fleet" is the mistake, not a shortcut to it; so is re-observing a
held run to check on it — that used to actively renew the hold by rewriting
the field its age was measured from, which is fixed, but it is why the
instinct persists.

**What is safe mid-hold:** `reckon crew observe` reads the local stream and
manifest and never calls the provider, so it works identically before and
during a refusal — there is no window where it must be run early or not at
all.

**The automatic path:** `reckon crew resume-ready --project <project>` sweeps
and resumes every run whose provider hold or declared external wait has ended;
`reckon crew follow` runs it itself on a two-minute cadence. The sweep, hold
state, lane probe and session resolver live together in
`reckon/crew/resumption.py`. A long-lived follower reloads the installed code
when it advances, preserving its process and session registration; a failed
reload reports explicitly in the pane.

Commands, verified with `--help`: `reckon crew observe`, `reckon crew resume
--advice`, `reckon crew resume-ready`, `reckon crew complete`, `reckon crew
recover` (a different capability — it classifies abandoned live pointers and
repairs the record only, it does not resume or promote). Full operational
detail: `~/.claude/skills/reckon-ship/references/outage-recovery.md`.

### Editing this package: two invariants a gate will not always catch

**The MCP surface is five tools.** `docs/AGENTS.md` states it and `_crew`'s own
docstring restates it — *deliberately one tool over eight views rather than
eight tools*. A new capability is a **view or an action on an existing tool**,
never a sixth tool. Measured 2026-09-04: a recovery surface was added as a sixth
top-level tool and `tests/test_ledger.py::test_the_mcp_surface_holds_at_five_tools`
went red on main and stayed red, because the assertion lives in a module that
sat outside the adding node's gate. The invariant was stated twice and its guard
was scoped away, so **put that test in the gate of any node touching
`reckon/mcp.py`.**

**Never name a new module after a callable this package already exports.**
`reckon/crew/` re-exports functions on the package, so a module created beside a
same-named callable makes `reckon.crew.<name>` resolve to the callable until the
submodule is explicitly imported and to the module afterwards — import-order
dependent identity. Measured 2026-09-04: a new `recover.py` beside the
long-standing `recover` callable turned six tests red at the merged head, and
every one had passed inside the worktree that produced them. It is now
`resumption.py`, and a test requires every submodule name outside
`_MODULE_EXPORTS` to resolve to a module, so the next collision of this shape
fails a test instead of turning main red. Check `_MODULE_EXPORTS` before adding
a module here.

### Fan-out boundary

In a crew-managed repository, investigation and review fan-out is `reckon crew dispatch` work under the investigate and review roles; harness-native background agents bypass the run ledger, manifests and calibration and are refused by the guard.

### Fleet sizing

The single advisory sizing table lives in `skills/reckon-ship/SKILL.md`. The
coordinator fills available slots with independent ready nodes and refills each
slot after its predecessor is verified; dependent work never builds on
unverified output.

Whenever a fleet is dispatched, the **Mandatory Sub-Agent Dispatch
Preamble** in `~/.agents/AGENTS.md` and Reckon's worktree dispatch contract are
binding. Where they differ, use the stricter rule and keep workers detached;
the orchestrator alone merges and pushes the primary branch.

### Runtime worker routing

Sprint and plan documents record task requirements, risk, autonomy, and
verification floors. They do not select workers. The current user prompt and
coordinator choose the concrete runtime model, reasoning effort, and
concurrency for each dispatched node and state those choices explicitly in the
worker prompt. Do not derive a relative model tier from the coordinator or
embed a provider/model preference in Reckon skills or plan state.

Legacy `tier`/`model_tier` metadata is compatibility input only. It must not
control runtime routing and should migrate to neutral capability requirements
when the containing resource is next edited.
