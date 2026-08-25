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
`watch.watcher_live`. When the latter is false, arm the returned line as a
harness `Monitor`; when it is true the seat belongs to someone else — possibly
to a producer dispatch armed detached on your behalf — and this session reaches
the same stream with `reckon crew follow --project <project>`, also as a
`Monitor`. Either way stdout is the signal: each line becomes a chat
notification, so a blocked or terminal transition wakes the orchestrator.
Sessions do not compete for the seat, so each keeps its own view and attaches or
leaves independently.

Both forms belong in a `Monitor`, and the follower especially, because it is a
**line-producing** primitive rather than an **exit-producing** one. It returns
only when `watcher_live` goes false — never on a terminal manifest, a stall
window, or an empty fleet. Arming it with a wake-on-exit background command
therefore yields silence until the seat dies, which reads as a quiet fleet and
is the measured cause of three sessions falling back to hand-rolled manifest
polling. The follower also refuses when no seat exists anywhere, naming the
arming line: it follows a watch, it does not start one.

Attaching late loses nothing. The follower yields a baseline built from the
current live pointers before seeking to the end of the stream, so it opens with
the present state of every run; only the historical lines themselves are not
replayed.

Do not launch the watcher as a detached shell background command. Its process
can remain live and satisfy the dispatch guard while no session reads its
stdout. This admitted four runs behind a seat armed eight hours earlier by a
different session; three terminal events then went unnoticed for more than two
hours. The monitor form prevents that measured failure by making every ticker
line observable instead of waiting only for process exit.

Neither `watcher_live` nor `seat_held` answers whether *this* session will be
woken: both are project-global, wake delivery is session-local, and dispatch
arms a producer detached on the caller's behalf, so both read true while the
caller hears nothing. Read them as "a seat exists", never as "I am attached".

### Filtering the ticker

The follower streams every transition, so the monitor needs a filter — and the
filter is where this goes wrong. Three sessions have now armed a broken one.

```
{ reckon crew follow --project <project> \
    | grep --line-buffered -E '→ +(complete|blocked|fail|stall|abandon|stop|unknown)' \
    || true; }
```

- **Anchor the state word to `→ `.** Every line ends with a summary field
  `· N blocked · N unpromoted`, so a bare `blocked` or `unpromoted` alternative
  matches the whole stream. An unanchored filter is a firehose that reads as a
  working channel until the monitor is stopped for volume.
- **`--line-buffered` on every stage**, or the ticker is withheld until exit.
- **`|| true`**, because a pipeline exits with its last stage's status and
  "nothing matched yet" would otherwise surface as a failing monitor.

Scope the filter to a node id as well when babysitting one long run; leave it
project-wide for a coordinator across waves, which is the only form that
reports work the session did not think to name.

`_watch_snapshot` emits exactly these states, and a filter is complete only
against this list:

| State | Emitted when |
|---|---|
| `complete` / `blocked` / `failed` | the manifest reports that status |
| `dispatched` | phase is `starting` |
| `working` | phase is `working` or `running` |
| `stalled` | `working` with the stream quiet past the stall window |
| `stopped` | phase is `stopped` — reachable from `reckon crew stop` |
| `unknown` | neither classification nor phase resolves |

Anything else arrives through the classification fallback, `abandoned` among
them. `dispatched` and `working` are the two a terminal filter omits on
purpose.

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
