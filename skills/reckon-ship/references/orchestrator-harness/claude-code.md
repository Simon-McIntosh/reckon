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

Two primitives on this host look interchangeable and are not. The difference
decides whether a finished worker reaches the session at all:

| Primitive | What it delivers | Right for |
|---|---|---|
| `Monitor` | one chat notification per **stdout line** | anything that emits lines and keeps running — the fleet follower |
| `Bash` with `run_in_background: true` | one notification when the command **exits** | a command with an end: a build, a test run, an until-loop |

**The follower belongs in a `Monitor`, and only there.** It is a
line-producing primitive, not an exit-producing one: it returns when its
session's watch is stopped — never on a terminal manifest, a stall window, or a
drained fleet. Backgrounding it as a shell therefore yields silence for as long
as the session lasts, and silence reads exactly like a quiet fleet.

```
Monitor({
  command: '/abs/path/to/reckon crew follow --project <project>'
         + ' --session <session> --lifetime 29m',
  description: '<project> <sprint-or-plan>',   // e.g. 'imas-codex S9'
  timeout_ms: 1800000,
})
```

**Arm the attach line verbatim, never a retyped command.** The monitor's
shell does not inherit this session's `PATH`, so a bare `reckon` there can exit
127 before the first line — and a monitor that dies inside a second reads as a
quiet fleet rather than as a failure.

The attach line reckon prints — `attach_line` in the dispatch payload, and the
same field in a `watcher-required` refusal — already names the entry point
absolutely, resolved beside the interpreter that composed it. Add
`--lifetime 29m`: the follower then ends itself a minute under the host's
thirty-minute cap, so the last line a reader sees is the follower's own, not
the host's expiry notice.

**Launch the follower with colour: never pass `--no-color`.** The pane this
host renders the ticker into reads ANSI colour, and the follower's colour set
carries the state of each row (the working, blocked, unpromoted and waiting
states are distinguished by hue). A monochrome stream flattens those states
into one grey line, which the reader then has to parse word by word. The lead
asked for colour explicitly on 2026-09-16 after a coordinator armed a
`--no-color` follower; `--theme dark|light` is the only appearance flag a
coordinator should pass, and only when the pane's background calls for it.

**What an agent reader gets is the escape sequences, not the colour.** Each
line is delivered as the body of a chat notification, so the same row a human
pane renders in hue reaches an agent as its own escape sequences wrapped around
the words; there is no renderer in between, and the colour is decoration rather
than a channel. The state word must carry the state alone — a state
distinguished only by hue is invisible to every reader who is not looking at
the pane.

**Make the description name the work, and stop.** This host prints it verbatim
as the visible row of every notification — `Monitor event: "<description>"` — so
it repeats identically for each event and never carries the transition. What it
*can* carry is the one thing known before the first event and true for all of
them: the target being executed. `imas-codex S9` for a sprint,
`nova hex-grid-derisk-gates` for a single plan. `<project> fleet` is not wrong
but it is not informative either — every follower on a project would render the
same row, and the target is what distinguishes this stream from the next one.

First-person prose there is worse than clutter, and the failure mode is
specific: a status claim that repeats unchanged across events reads as a
*liveness assertion*, and an unchanging one reads as a stale assertion. Measured
— a row saying `imas-codex fleet: my session's runs, all transitions`, repeating
identically, led its reader to ask whether the follower was a dead process. A
healthy stream had invited exactly the wrong diagnosis. The row is the reader's
transcript, not the follower's; the event text is in the notification body, and
the description's whole job is to say which stream a row came from.

**Measured on this host, which is why reckon can check it rather than ask.** A
backgrounded shell's stdout is a regular file that the harness reads when the
command exits; a monitor's is a socket a reader consumes line by line. Reckon
classifies the descriptor its lines are written to and registers a
file-terminated follower as *not* delivering, so the next dispatch is refused
with `watcher-required` naming the command to arm. A filter in between does not
disguise it: the pipe is followed to the process on its other end, so
`follow | grep > file` is judged by where the lines stop.

The failure this replaced: four runs behind a seat armed eight hours earlier by
a different session; three terminal events then went unnoticed for more than
two hours, and three sessions independently fell back to hand-rolled manifest
polling loops. A shell-armed follower produced every one of those.

### One producer, one follower per session

Dispatch arms the project's producer detached, on your behalf. It is shared, it
is not yours, and `watcher_live` says only that it exists. Your own follower is
`--session <session>` — the same session id you pass to `reckon crew dispatch`
— which both scopes the stream to your runs and registers the session as
attached so dispatch can verify delivery. Sessions do not compete: each keeps
its own view and attaches or leaves independently.

Arm it **before** the first dispatch if you like; a follower with no producer
waits for one rather than refusing, and re-attaches by itself when a later wave
arms a fresh seat. Attaching late loses nothing either — it opens with a
baseline of every live run, then streams.

To carry a peer's runs beside your own — after a handover, or while a peer
coordinator is working the same sprint — name them on the same `Monitor`, one
`--observe-session` per session. Never open a second `Monitor` for them:

```
Monitor({
  command: '/abs/path/to/reckon crew follow --project <project>'
         + ' --session <yours> --observe-session <peer-a>'
         + ' --observe-session <peer-b> --lifetime 29m',
  description: '<project> <sprint-or-plan>',
  timeout_ms: 1800000,
})
```

**This host expires a monitor and the expiry is not a fleet event.** The notice
reads `Monitor expired after 30m with N events delivered`, and from that moment
the session has no delivering follower — so the next `reckon crew dispatch` is
refused with `watcher-required` (exit 8), which is the first many coordinators
learn of it. `timeout_ms` is capped at 1,800,000 milliseconds — thirty minutes
— so a session outlasting that cap **will** meet this at least once. A follower
armed with `--lifetime 29m` ends itself first, and **its final
line is the one to re-arm on**: marked as the follower's end rather than a
fleet event, it names the attach line to arm and the owning session's runs that
need the coordinator, and it releases the registration on the way out. Re-arm
on it before anything else, with the same flag set including every
`--observe-session`; a follower that re-attaches to a session holding a record
replays the runs
whose state changed since it, so nothing that happened in the gap stays hidden.

### Filtering

Do not build a shell filter, and do not add a state filter by default. The
follower reports every transition — starts, `working`, landings, promotions —
because a filter that legitimately matches nothing gives this host a pane
reading `No output available`, which is indistinguishable from a follower that
died. Measured: a session with two healthy runs sat like that for minutes.

`--run <id>` narrows to named runs, selecting on the transition's own fields,
which is why it cannot repeat the three shell-filter mistakes: withholding
lines in a stage's buffer, matching the `· N blocked · N unpromoted` summary
that trails every line, or hiding a refusal behind `|| true`.

The column after the clock is the model and effort that ran the node, read from
the configuration persisted at dispatch rather than from current flight config —
so a later config change cannot silently restate what ran. It sits with the
timestamp because a reader scanning a wave compares agents down a column, and it
is what distinguishes a stall in the model from a stall in the work.

The three figures partition the fleet as it stands after each line: `working` is
work in progress, `blocked` is everything stopped and needing the coordinator (a
stall or a failure included), and `unpromoted` is delivered work awaiting a gate.
They add up to the runs in flight, which is why none of them is called `live` —
a pointer count in that position is read as work in progress and is not.

Attaching opens with one line per live run, so the pane is never blank while
work exists:

```
12:30:11  <model>/<effort>   hdg-cache-replay    → dispatched        2 working · 0 blocked · 0 unpromoted
12:30:11  <model>/<effort>   hdg-measured-map    → working           2 working · 0 blocked · 0 unpromoted
12:30:12  <model>/<effort>   hdg-measured-map    working → blocked   2 working · 1 blocked · 0 unpromoted · tried: …
```

**The stream carries worker transitions and fleet posture, and nothing else.**
No follower status, no arming advice, no registration chatter — a reader is
watching a fleet, not a follower, and two streams squeezed into one pane is
worse than either. Nothing goes to stderr either: this host prefixes it with
`[stderr]` into the same pane, which is noise, and it splits one sequence across
two interleaved channels that then appear to contradict each other. Whatever the
follower needs to say about its own registration is said by the dispatch guard,
to the session that is trying to dispatch, with the remedy. Clocks are the
reader's local time, since the pane sits beside a harness that timestamps
locally.

Leave `--session` off only to watch a project's whole fleet across sessions;
every line then names its owning session, and the runs it reports are not
yours to act on.

Neither `watcher_live` nor `seat_held` answers whether *this* session will be
woken: both are project-global, wake delivery is session-local, and dispatch
arms a producer detached on the caller's behalf, so both read true while the
caller hears nothing. `session_attached` is the field that answers it.

## Resuming a held wave without a human

A wave held on a reset timestamp knows *when* it can reopen —
`reckon crew preflight` reports `resume_after_seconds` and `resume_at`. Turning
that into an actual resumption is the harness-local part, and this host offers
three forms, in increasing order of what they cost and commit to.

**Read this section as the narrow exception it is.** These forms exist because a
quota reset is a *time*, and no fleet transition will ever fire for it, so the
follower cannot deliver it. Everything the follower CAN deliver — a run
finishing, blocking, dying, changing state at all — is delivered by the follower
and must never be waited on with a shell. Never background a command to find out
what a worker is doing, never re-run a check on a timer, and never sleep to let a
run progress: the follower already pushes that transition, and a poll spends a
whole turn to read state it pushed. SKILL.md rule 18 is the rule; this list is
only for the case the follower structurally cannot cover.

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
