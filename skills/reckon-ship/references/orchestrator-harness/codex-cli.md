# Orchestrator harness — Codex CLI

**Harness-local, and quarantined for exactly that reason.** Every sentence here
turns on a capability of the *host* harness the orchestrator itself runs inside.

**Ownership test:** the same as its sibling file — a sentence that does not turn
on a capability of *this* host harness is misfiled.

This file exists as much for what it records absent as for what it records
present. The two hosts are close to complementary, and an orchestrator that
assumed one host's capabilities on the other would either stall waiting for a
wake-up that never comes or dispatch into a quota it could have checked.

## Capabilities

| Capability | Present | How |
|---|---|---|
| Background dispatch | partly | A command can be detached with the harness's shell, but nothing binds the detached process to the session, so the orchestrator owns the bookkeeping itself. `reckon crew dispatch` already does exactly that, which is why a spawned worker is unaffected by this gap. |
| Wake on completion | no | A non-interactive turn ends when the model stops. Nothing re-invokes the session when a process exits, so a wave cannot be resumed from inside; poll within the turn, or hand the resumption to an external scheduler. |
| Self-scheduling | no | The harness offers the orchestrator no primitive to schedule its own next turn. Resuming a held wave needs an external trigger — a user-owned timer or service invoking a fresh session — and setting one up is the lead's call, not the orchestrator's. |
| Budget visibility to itself | yes | Its account surface answers a limits read over the app server's line protocol, and that read runs no model. Reckon already speaks it: set `budget_check: true` on the backend in flight config and the pre-flight reads live headroom instead of relying on what earlier runs recorded. |

## Resuming a held wave without a human

Not available on this host. A wave held here reports the hold, its utilisation and
its reset time, and stops — the degraded path its sibling file describes, taken
as the only path.

Two consequences worth stating, because both are easy to get backwards:

- **Do not substitute polling for scheduling.** Holding the turn open until the
  reset spends the orchestrator's own context to wait, and loses everything if
  the turn ends first. Report the reset and stop.
- **This host is the better one to read headroom from, not the worse one.** Its
  account surface gives a live figure where the other host gives none, so a
  pre-flight run here can be more accurate than one run elsewhere — the asymmetry
  is in scheduling, not in visibility.
