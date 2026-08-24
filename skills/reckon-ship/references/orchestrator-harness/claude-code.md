# Orchestrator harness — Claude Code

**Harness-local, and quarantined for exactly that reason.** Every sentence here
turns on a capability of the *host* harness the orchestrator itself runs inside.
Naming one of these in the skill or in a process reference would silently couple
the whole skill to one host, which is the portability a single skill exists to
preserve.

**Ownership test:** a sentence here that does not turn on a capability of *this*
host harness is misfiled. Process rules belong in `sprint-orchestration.md`;
what a worker does belongs in `worker-protocol.md`; how a worker is launched
belongs in `worker-backends.md`; routing and threshold policy belong in flight
config.

## Capabilities

| Capability | Present | How |
|---|---|---|
| Background dispatch | yes | `Bash` with `run_in_background: true` detaches a command so it survives the turn. Subagents dispatched with `Agent` run in the background by default. |
| Wake on completion | yes | The session is re-invoked when a backgrounded command exits or a dispatched agent finishes; `Monitor` with an until-loop waits on a condition without burning turns. |
| Self-scheduling | yes, three forms | See below. Which one is available depends on how the session was started. |
| Budget visibility to itself | no | This harness exposes the orchestrator no machine-readable account headroom for its own session. Treat the orchestrator's own budget as unknown, and never infer it from a worker backend's figures. |

## Arming the fleet watch after dispatch

Every successful dispatch returns `watch.arming_line` and
`watch.watcher_live`. When the latter is false, pass the returned line to
`Bash` with `run_in_background: true`; when it is true, launch nothing. The
background command exiting is the wake event this harness already observes, so
the portable watcher must stay harness-owned rather than detaching itself.

A settings automation uses the same two-field contract: a `PostToolUse` hook
matching `Bash` inspects successful `reckon crew dispatch` JSON, ignores results
whose `watch.watcher_live` is true, and submits the exact `watch.arming_line` as
a background `Bash` call. The hook does not reconstruct the command from the
dispatch arguments and does not create one watcher per run. Keep this automation
in Claude Code settings; it depends on this host's wake-on-background-completion
behavior and does not belong in the portable skill.

## Resuming a held wave without a human

A wave held on a reset timestamp knows *when* it can reopen —
`reckon crew preflight` reports `resume_after_seconds` and `resume_at`. Turning
that into an actual resumption is the harness-local part, and this host offers
three forms, in increasing order of what they cost and commit to:

1. **A detached wait, then the check.** Background a command that sleeps until the
   reset and then re-runs the pre-flight; the harness re-invokes the session when
   it exits, and the run log holds the verdict. Needs no session mode and creates
   nothing outside the workstation, so this is the default form.
2. **`ScheduleWakeup`.** Available only when the session is running self-paced,
   and it carries the prompt to re-enter on waking. Prefer it in that mode: the
   wake-up is the loop's own, not a process the loop has to watch.
3. **`CronCreate`.** A durable scheduled agent that survives the session ending.
   It is outward-facing — it creates a standing routine under the user's account
   — so it needs the lead's explicit authorisation, and is the right form only for
   a hold that outlasts the session.

Whichever form is used, report the resumption on the hold's own four axes when it
fires: elapsed wait, the verdict the pre-flight returned, and what the wave then
did. A resumption nobody can see is as opaque as the hold it lifted.

**When none of the three is available**, the orchestrator reports the reset time
and stops. That is degraded, not broken: the hold, its figure and its reset are
all recorded, and a human resumes with one invocation. A harness that cannot
schedule itself must never respond by dispatching into a spent quota instead.
