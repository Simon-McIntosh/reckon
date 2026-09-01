# Worker backends — maintainer notes

**Not agent-facing.** An orchestrator never needs this file: it issues one
dispatch instruction and reads the returned launch kind. This describes what
`reckon/_backends.py` does internally, for whoever changes it.

**Ownership test:** a sentence here that does not turn on process, session or
observation mechanics is misfiled. Anything about what a worker should *do*
belongs in `worker-protocol.md`; anything about routing *policy* belongs in
flight config.

## Why the translation is contained

`reckon/_backends.py` is the only module that speaks a harness's dialect. Because
per-backend flags live there and nowhere else, no skill, plan or prompt can name
one — so two execution paths cannot drift apart by wording. A difference between
backends is either in that file or it does not exist.

That containment is also why it is the one module exempt from the ban on naming
harnesses and models: the ban protects the surfaces an agent reads, and
translation is not one of them.

## The single branch

Callers branch once, on `launch` kind, and adding a harness never adds a third
case:

- **`cli`** — an external process reckon can spawn. `dispatch` creates the
  worktree, launches it, writes the live pointer and returns a run id; the caller
  backgrounds that and yields.
- **`in-harness`** — the calling harness's own delegation primitive, which reckon
  cannot spawn on its behalf. So reckon prepares the worktree, manifest path and
  resolved fences, returns a dispatch directive, and the caller binds its own
  task back with `reckon crew attach`. Calling `launch_plan` on such a backend
  raises rather than silently substituting another backend, because substituting
  one would hide the misrouting.

## What a dialect owns

A dialect is selected by the backend's **command**, not its name: the name is
free-form user data — a config may call a backend `fast` or `reviewer` — while
the command is the executable whose flags have to be spoken. An unknown command
raises and lists what can be translated; there is no generic fallback, because a
guessed flag vector is worse than a stopped run.

Each dialect supplies exactly three things:

**Argument construction.** The machine-readable stream flag, the worktree
directory, the model and effort identifiers as free text from config, and the
sandbox tier. Tier mapping is per-dialect: the `worktree-full` tier resolves to
whatever that harness calls "no sandbox", because a filesystem sandbox is
inherited by child processes and breaks the test runners a worker's gate depends
on; the worktree is the boundary instead. A restrained tier resolves to whatever
that harness offers — a sandbox mode in one, a permission mode in another. For
the filesystem-sandbox dialect, read-only work translates to workspace-write
with the manifest directory as the working directory. That leaves the delivery
surface writable without making the assigned repository writable.

**A restrained tier needs the roots that sit outside the workspace, named
explicitly.** The filesystem-sandbox dialect takes `--add-dir <DIR>` for
"additional directories that should be writable alongside the primary
workspace", and a worktree node needs two of them, because two things it must
write are not inside the worktree:

```
<harness> exec -s workspace-write -C <worktree> \
  --add-dir <main-repo-root> \
  --add-dir <config-home>/crew/runs/<run-id> ...
```

The main repository root because a detached worktree's git index lives at
`<main-repo>/.git/worktrees/<node>/index`, so without it `git commit` fails on
`index.lock` after the node's work is already done; and the manifest's directory
because the delivery path is outside the worktree too. `sandbox_write_roots`
computes exactly this set — repository root, run directory, reports directory,
temporary directory, manifest parent — which is why a dispatch at a restrained
tier commits and a hand-composed line at the same tier does not. Reproduce the
roots, not the tier name; escalating to the full-access tier instead grants more
privilege than dispatch would, and that is a decision rather than a shortcut.

Two argument details are load-bearing and were both learned by a failed probe:

- **The prompt travels on stdin, for every dialect.** A prompt passed as a
  positional argument can be swallowed by a preceding variadic option, and the
  harness then reports that no input was provided while the prompt sits in its
  argument list. One dialect additionally needs a trailing marker to say the
  prompt is on stdin.
- **The working directory is part of the sandbox boundary.** Full-access and
  workspace-write nodes run in their worktree. Read-only nodes using the
  filesystem-sandbox dialect run in the manifest directory instead, so only the
  delivery surface and system temporary directory gain write access. The
  translated launch explicitly permits that delivery directory to be outside a
  repository; delivery locations do not need repository metadata.

**Session capture.** The resumable id, taken from the stream's own start event.
This is what lets a worker's session outlive its workspace, and it is what the
escape hatch depends on: advice is resumed into the same session so the worker
still remembers what it tried. One dialect exposes the id on a dedicated start
event; another repeats it on every event, including events a host's hook
configuration emits before the session's own init.

**Stream interpretation.** Folding the event stream into one `Observation` —
phase, terminal status, final message, budget. Phase is derived rather than
stored: no events means the process has not reported yet, a terminal event means
it finished, and anything between is work in progress. A stream that stops
without a terminal event therefore reads `working` forever, which is correct —
only the process table can distinguish a slow worker from a dead one, so
liveness is recorded beside the stream and a dead process with no terminal event
is reported as a recoverable orphan.

**Read the verdict from the error flag, never from a status label.** One harness
labels a failed turn `subtype: "success"` while setting its error flag on the
same event. Keying off the label inverts the verdict, which is why the recorded
failure streams are fixtures rather than a comment.

## Budget is asymmetric, and must stay that way

The harnesses disagree about what their run streams report, and the design must
not pretend otherwise:

- one emits a structured rate-limit event carrying utilisation, a reset time and
  a threshold status — enough to reason about headroom;
- another emits per-turn token usage and no headroom at all.

So the normalised block carries `headroom: "known"` or `headroom: "unknown"`, and
`budget_exhausted()` answers `True`, `False` or `None`. **`None` is the point.**
Token counts are not a budget: a record showing spend and no headroom looks, on
any single field, like a record showing plenty. A caller reading fields directly
can conclude "empty" from silence and stop a wave that had budget left, so
absence is never read as exhaustion.

Capturing whatever is emitted from the first dispatch onward is what gives later
work a history to reason over. Deciding what to do with the signal belongs to
`reckon/budget.py`, not here.

### The asymmetry is in the stream, not always in the harness

A probe established that the harness whose *stream* carries no headroom does
publish it elsewhere — over its own account surface, on a different transport,
answered by a read that runs no model. So a dialect may also supply a
`BudgetProbe`: an argument vector plus the requests to write to a held-open stdin
and the id of the answer to wait for. Two mechanics were learned by that probe and
are why this is a probe rather than a command whose output is read:

- the server rejects requests until a handshake has been answered, so the exchange
  is a sequence rather than one call;
- it exits the moment its input closes, so a `command < requests` redirect returns
  nothing at all — stdin stays open for the life of the exchange, and the reply is
  read on a thread because unrelated notifications arrive interleaved with it.

A dialect whose stream already carries headroom declares no probe: a second
process would learn nothing the run did not already report. And every failure path
— no dialect, no probe, no answer, a broken exchange — returns an unknown block
naming the reason instead of raising, because an instrument that fails must not
become a hold.

## Recorded fixtures

Every observation path is tested against streams captured from live runs, under
`tests/fixtures/backends/` — success and failure per dialect, with a README
recording their provenance and the two elisions. No test spawns a process or
reaches a network.

A test asserts that every dialect has a recorded stream, so a dialect added
without a fixture fails rather than shipping untested.

## Adding a dialect

1. Probe the harness live and record its success and failure streams as fixtures.
2. Add a `Dialect` subclass supplying argv, the tier mapping, the resume form and
   `observe`; register it under its command name.
3. Parameterise the existing translation tests over it — they are written per
   dialect precisely so a new one inherits the whole matrix.
4. Add the backend to a flight config layer as data. Nothing outside this module
   and that config changes, and no agent-facing text mentions it.
