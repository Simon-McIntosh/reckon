# Recovering from a provider outage

**Agent-facing.** Read this the moment a dispatched run reads `blocked`, before
touching `reckon crew complete` or `reckon crew resume`. It exists because the
order below was recorded once already and a different coordinator, four hours
later, did not apply it — the guidance had to be recalled at the exact moment
an incident made recall unlikely, and it failed both times it was needed.

## The cause

A per-request provider refusal kills the worker's turn without killing its
session. The run's manifest is absent or stale, so the live classifier reads
`blocked` — but the process that refused was the provider, not the harness: the
worktree is exactly as the worker left it, and the session transcript is still
on disk, resumable.

## The trap

A live pointer's `session_id` reading empty means only that `observe` has not
run against this pointer yet. It is not a verdict that the run is unresumable —
`resume_plan` recovers a missing id from the run's own stream without being
told where to look. Two coordinators read that empty field as "gone" on the
same day: the first promoted five blocked-but-recoverable runs, destroying
their resume paths in the same act that marked them done; the second left one
run blocked for five hours and forty-two minutes after its refusal had already
lifted, because nothing re-checked it.

## The order

1. **Inspect the worktree** for committed and uncommitted work before anything
   else — `git -C <worktree> status --porcelain` and `git -C <worktree> log
   <base_sha>..HEAD`.
2. **Resume the session** — `reckon crew resume --run <run-id> --advice
   "<answer>"` continues the same session with its prior context intact.
3. **Reconcile only once resume is impossible.** Promotion deletes the live
   pointer, and the pointer (or, failing that, the stream) is where a resume
   finds the session to continue. Promoting first forecloses the option to
   resume — it cannot be recovered by trying to resume afterward.

## The two counter-instincts, and both are wrong

- **"Promote first, to tidy the fleet, then sort out the blocked ones."**
  Promotion is exactly the act that destroys what you would otherwise recover.
  A blocked run with a recoverable session is not clutter; it is unfinished
  work still holding its own context.
- **"Re-observe a held run to check on it while investigating a hold."** This
  used to actively make the situation worse: the ageing rule that lets a hold
  expire once measured a run's age from the live pointer's `observed_at`
  field, and `observe` rewrote that field on every call — so checking on a
  held run re-stamped its refusal as fresh and the hold never lapsed on its
  own. That specific defect is fixed (age is now taken from the run's
  immutable `created_at`), but it is why the instinct to avoid re-observing a
  held run persists, and it is worth knowing the instinct no longer has a
  reason.

## What is safe during a hold

`reckon crew observe` reads the run's local stream and manifest; it never
calls the provider. It works identically whether a refusal is currently live
or has already lifted, confirmed by resuming a session mid-hold with no
preparation and recovering its id from the stream alone. The belief that
`observe` must run *before* a limit lands, because it cannot run *after*, is
not correct and teaches a reader to panic at exactly the moment panic is
unwarranted.

## The automatic path

`reckon crew resume-held --project <project>` sweeps every run in a project
still held by a lapsed provider refusal and resumes each with a continue
advice, bounded at one resume per hold. It is idempotent and reads only
records already on disk. `reckon crew follow` runs this sweep itself on a
two-minute cadence, so a coordinator attached to its own fleet needs to notice
nothing.

**A follower no longer needs cycling, and this is a change from earlier
guidance.** A `crew follow` process used to hold the module code it started
with for its entire life, so a follower already running when the sweep landed
would never call it — measured once at five hours and forty-two minutes of a
run left blocked after its refusal had lifted, on two separate sessions'
followers simultaneously. The follower now detects that the installed package
has advanced and re-execs itself in place, keeping its session registration and
its stream unbroken. **Do not restart a follower after a merge.** If one cannot
re-exec it says so explicitly in the pane rather than continuing quietly, and
that message is the signal to intervene — not the passage of time since you
started it.

## The commands

Verify these with `--help` before relying on them — the exact flags move as
this area of the codebase is actively worked:

```
reckon crew observe --run <run-id> [--project <project>]
reckon crew resume --run <run-id> --advice "<answer>" [--print-only]
reckon crew resume-held --project <project> [--dry-run]
reckon crew complete --run <run-id> --gate <verdict> --commit <sha> ...
reckon crew recover [--project <project>]
```

`reckon crew recover` classifies every live pointer left behind by an
interrupted orchestrator (running, completed-but-unpromoted, or abandoned) and
repairs the record only; it does not resume, promote, or apply the ordering
above on its own. Do not confuse it with the resume-first order this file
describes.

`reckon crew complete` refuses to promote a run classified `blocked` whose
session is still recoverable, naming the resume command instead.

**That refusal names a waiver the command does not yet accept.** The refusal
text offers `--waive-resume-path REASON`, and the parameter exists in the
promotion path, but it is not wired to the CLI — so a coordinator who follows
the refusal's own advice reaches a dead end. Until it is wired, the only ways
past the guard are resuming the session or reconciling it another way; do not
spend time hunting for the flag. This is being fixed, and when it lands the
waiver's reason will be recorded on the promoted row, so a deliberately
discarded session is afterwards distinguishable from an accidental one.
